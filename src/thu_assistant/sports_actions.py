"""Sports reservation previews, single dispatch, and independent order readback."""
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from .actions import RequestSpec, NotDispatched, digest, sports_enabled
from .bridge import CampusError
from . import sports as s
from .sports_transport import Reader, WIRE, USER, CAPTCHA, CREATE, CANCEL, ORIGIN, PREFIX, PERSONAL, HOME, SIGN, FORM_BRIEF


def inputs(business, operation):
    keys = ('scene_id','room_id','site_id','session_id','date','mode','members','payment_method') if operation == CREATE else ('record_id',)
    return {k:business[k] for k in keys} | ({'expected_total_fen':business['expected_total_fen']} if operation==CREATE and 'expected_total_fen' in business else {})


def all_orders(reader):
    result = []
    ids = set()
    previous_total = None
    for page in range(1,51):
        rows, scope = s.order_page(reader,page,50)
        current = {str(row['resvUuid']) for row in rows}
        s.require(not ids.intersection(current) and previous_total in (None,scope['upstream_total']), 'SPORTS_ORDER_LIST_CHANGED')
        ids.update(current)
        result.extend(rows)
        if not scope['next_page']:
            return result
        previous_total = scope['upstream_total']
    raise CampusError('SPORTS_ORDER_SEARCH_INCOMPLETE')


def selected_slot(reader, arguments):
    page = 1
    while page <= 50:
        selected = {key:arguments[key] for key in ('scene_id','room_id','mode','date')}
        result = s.availability(reader,{**selected,'page':page,'limit':50})
        if not result['complete']:
            raise CampusError('SPORTS_SLOT_NOT_VERIFIED')
        for site in result['items']:
            if site['site_id'] == arguments['site_id']:
                matches = [x for x in site['sessions'] if x['session_id'] == arguments['session_id']]
                s.require(len(matches)==1,'SPORTS_SESSION_NOT_FOUND')
                s.require(sum((x['start_at'],x['end_at'])==(matches[0]['start_at'],matches[0]['end_at']) for x in site['sessions'])==1,
                          'SPORTS_SESSION_TIME_AMBIGUOUS')
                return result['scene'], site, matches[0]
        if not result.get('next_page'):
            break
        page = result['next_page']
    raise CampusError('SPORTS_SITE_NOT_IN_SELECTED_ROOM')


def creation_preview(reader, args):
    scene, site, slot = selected_slot(reader,args)
    s.require(slot['available'] is True,'SPORTS_SLOT_UNAVAILABLE')
    s.require(slot['unit_fee_fen'] is not None,'SPORTS_FEE_NOT_VERIFIED')
    member = reader.call(USER)['data']
    member_id = s.ident(member.get('id'))
    captcha = reader.call(CAPTCHA)['data']
    s.require(captcha.get('sysKey')=='RESV_CODE' and str(captcha.get('sysValue')) in ('0','1'), 'SPORTS_VERIFICATION_RULE_UNKNOWN')
    fee = slot['unit_fee_fen']
    s.require(args['payment_method'] != 'FREE' or fee == 0,'SPORTS_FEE_CHANGED')
    if args['payment_method']=='FREE' and args.get('expected_total_fen',0)!=0:
        raise CampusError('SPORTS_FEE_CHANGED')
    checks = []
    if str(captcha['sysValue']) == '1':checks.append('OFFICIAL_CAPTCHA')
    deployment = None
    if site['form_id']:
        brief = reader.call(FORM_BRIEF+s.ident(site['form_id'])).get('data')
        s.require(isinstance(brief,dict) and brief.get('deployUuid'),'SPORTS_FORM_DEPLOYMENT_UNKNOWN')
        deployment = s.ident(brief['deployUuid'])
    if site['form_id'] or not site['form_state_known']:checks.append('OFFICIAL_FORM')
    total = slot['quoted_total_fen']
    if total is not None and (args['payment_method']=='FREE' and total!=0 or
                              'expected_total_fen' in args and total!=args['expected_total_fen']):
        raise CampusError('SPORTS_FEE_CHANGED')
    if fee or total is None or args['payment_method']!='FREE':checks.append('CONFIRM_TOTAL_AND_PAYMENT_METHOD')
    if args['members'] != 'self':checks.append('CONFIRM_PARTICIPANTS')
    if args['mode'] == 'SPORT_PERSON':checks.append('CHOOSE_TICKET_AND_QUANTITY')
    rules = site['cancel_rule']
    if all(value is None for value in rules.values()):checks.append('CONFIRM_CANCELLATION_RULE')
    current_orders = all_orders(reader)
    for raw in current_orders:
        order = s.normalize_order(raw)
        if order['payment_status'] == 'PENDING_PAYMENT':
            raise CampusError('SPORTS_EXISTING_UNPAID_ORDER')
        if (order['scene_id'] == args['scene_id'] and order['start_at'] == slot['start_at']
                and args['site_id'] in [x['site_id'] for x in order['sites']]
                and order['status'] not in ('CANCELLED','COMPLETED')):
            raise CampusError('SPORTS_EXISTING_MATCHING_RESERVATION')
    business = {**args,'target':scene['name']+' / '+site['name'],'start_time':slot['start_at'],'end_time':slot['end_at'],
                'account_member':member_id,'fee':f'平台场次单价 {fee/100:.2f} 元' + ('，应付总额以结算页为准' if fee else ''),
                'cancel_rule':'平台取消提前量（分钟）：'+str(rules['cancelBeforeStart']) if rules['cancelBeforeStart'] is not None else '取消期限须在官方页面核实',
                'handoff':','.join(checks),
                'official_url':HOME+('#/swim' if args['mode']=='SPORT_PERSON' else '#/sports'),
                'fingerprint':digest([slot['charging_mode'],fee,total,site['form_id'],deployment,rules,slot['payment_hint']])}
    span = {'startTime':slot['start_at'][:19].replace('T',' '),'endTime':slot['end_at'][:19].replace('T',' ')}
    body = {'sceneUuid':args['scene_id'],'siteUuid':args['site_id'],'siteType':site['site_type'],
            'sceneUseType':args['mode'],'reserveTime':[span],'siteSessionReserve':[{'sessionDetailUuid':args['session_id'],'reserveTime':span}],
            'resvMember':[member_id],'resvKind':'CURRENT_RESERVE','payType':'PAY_ONLINE' if args['payment_method']=='FREE' else args['payment_method'],
            'purchaseUuid':'','formParam':{'formId':'','deployUuid':'','variables':{},'chooseCandidates':{}},'captcha':''}
    return business, body


def cancel_preview(reader, args):
    raw, detail = s.own_order(reader,args['record_id'])
    row = s.normalize_order(raw)
    s.require(detail['data'].get('resvExtendStatus') == row['extended_status'],'SPORTS_RECORD_CHANGED_REFRESH')
    s.require(row['status'] in ('SCHEDULED','PENDING_PAYMENT','PENDING_APPROVAL')
              and datetime.fromisoformat(row['start_at']) > datetime.now(s.TZ),'SPORTS_CANCELLATION_NOT_AVAILABLE')
    # Refunds, individual tickets, and uncertain controls belong on the
    # official confirmation page where the platform presents exact consequences.
    handoff = []
    if (row['platform_status']!='RESV_UNEFFECT' and row['approval_status']!='RESV_WAIT_APPROVE'
            or type(row['extended_status']) is int and row['extended_status'] & 256
            or row['payment_status']=='PROCESSING'):
        raise CampusError('SPORTS_CANCELLATION_NOT_AVAILABLE')
    if row['mode'] != 'SPORT_GROUP' or type(raw.get('resvProperty')) is not int or raw['resvProperty'] != 0:
        handoff.append('OFFICIAL_CANCEL_CONTROL')
    paid = row['paid_amount_fen']
    cutoff = row['cancel_before_start_minutes']
    if type(paid) is not int or paid != 0 or row['refund_status'] != 'NONE' or row['payment_status']=='UNKNOWN':
        handoff.append('CONFIRM_REFUND_CONSEQUENCES')
    if type(cutoff) is not int or not 0<=cutoff<=525600 or datetime.now(s.TZ) >= datetime.fromisoformat(row['start_at'])-timedelta(minutes=cutoff):
        handoff.append('CONFIRM_CANCELLATION_DEADLINE')
    business = {**args,'target':row['scene_name']+' / '+'、'.join(x['name'] for x in row['sites']),
                'scene_id':row['scene_id'],'site_ids':json.dumps([x['site_id'] for x in row['sites']]),
                'start_time':row['start_at'],'end_time':row['end_at'],'original_status':row['status'],
                'fee':f'已付 {paid/100:.2f} 元' if type(paid) is int else '已付金额待官方核实',
                'refund_state':row['refund_status'],'cancel_rule':f'平台取消提前分钟数 {cutoff}；退款结果独立核对',
                'official_url':PERSONAL,'handoff':','.join(handoff),'fingerprint':digest([row['extended_status'],raw.get('resvProperty'),cutoff,paid])}
    return business, {'resvUuid':args['record_id']}


def dispatch_exact(reader, path, body, gate):
    """Bind the official signed URL to the one allowed unsigned business request."""
    if path not in (CREATE,CANCEL):
        raise NotDispatched()
    prepared = reader.page.evaluate(WIRE, {'path':path,'method':'POST','params':body,'prepare':True})
    url = urlsplit(prepared['url'])
    canonical = ORIGIN+PREFIX+path
    if (url.scheme+'://'+url.netloc+url.path != canonical or set(k for k,_ in parse_qsl(url.query)) != SIGN
            or json.loads(prepared['body']) != body):
        raise NotDispatched()
    def guard(route):
        req = route.request
        if (req.url == prepared['url'] and req.frame == reader.page.main_frame and
                gate.consume(canonical,req.method,req.post_data)):
            return route.continue_()
        return route.abort()
    # One short-lived page route, bound to the exact signed URL, method, frame
    # and body. Never grant an origin-wide exception or use an HTTP retry.
    import re
    pattern = re.compile('^'+re.escape(prepared['url'])+'$')
    reader.page.route(pattern,guard)
    try:
        reader.page.evaluate("""async () => {
          const p=window.__thuSportPending;delete window.__thuSportPending;
          const c=new AbortController(),t=setTimeout(()=>c.abort(),18000);
          try{await fetch(p.url,{...p.init,signal:c.signal})}finally{clearTimeout(t)}
        }""")
    finally:
        reader.page.unroute(pattern,guard)
    if not gate.sent:
        raise NotDispatched()


class SportsAction:
    version = 'sports-action-v1'

    def __init__(self,browsers,path):
        self.browsers, self.path = browsers, path

    def read_preview(self, reader, args):
        fn = creation_preview if self.path == CREATE else cancel_preview
        return fn(reader, inputs(args,self.path))

    def preview(self, args):
        with Reader(self.browsers) as reader:
            return self.read_preview(reader,args)[0]

    def preflight(self, record):
        s.require(sports_enabled(),'SPORTS_WRITES_DISABLED')
        with Reader(self.browsers) as reader:
            s.require(reader.session['account_id']==record['account'],'ACTION_ACCOUNT_MISMATCH')
            business, body = self.read_preview(reader,record['business'])
        flags = set(business['handoff'].split(',')) if business['handoff'] else set()
        checks = {'availability':True,
                  'eligibility':not bool(flags & {'OFFICIAL_CAPTCHA','OFFICIAL_FORM','CONFIRM_PARTICIPANTS','CHOOSE_TICKET_AND_QUANTITY','OFFICIAL_CANCEL_CONTROL'}),
                  'fees':not bool(flags & {'CONFIRM_TOTAL_AND_PAYMENT_METHOD','CONFIRM_REFUND_CONSEQUENCES'}),
                  'rules':not bool(flags & {'CONFIRM_CANCELLATION_RULE','CONFIRM_CANCELLATION_DEADLINE'})}
        return {'business':business,'context':None,'checks':checks,
                'official_handoff':business['handoff'] or None,
                'requests':[RequestSpec(ORIGIN+PREFIX+self.path,'POST',json.dumps(body,ensure_ascii=False))]}

    def dispatch(self, record, gate):
        with Reader(self.browsers) as reader:
            if not sports_enabled() or reader.session['account_id'] != record['account']:
                raise NotDispatched()
            business, body = self.read_preview(reader,record['business'])
            if (business != record['business'] or business['handoff'] or time.time()>=record['expires_at']
                    or time.time()>=record['authorization']['evidence']['expires_at']):
                raise NotDispatched()
            dispatch_exact(reader,self.path,body,gate)

    def reconcile(self, record):
        business = record['business']
        records = []
        with Reader(self.browsers) as reader:
            s.require(reader.session['account_id']==record['account'],'ACTION_ACCOUNT_MISMATCH')
            if self.path == CANCEL:
                raw, detail = s.own_order(reader,business['record_id'])
                row = s.normalize_order(raw)
                if (row['status']=='CANCELLED' and detail['data'].get('resvExtendStatus')==row['extended_status']
                    and row['scene_id']==business['scene_id'] and row['start_at']==business['start_time']
                    and row['end_at']==business['end_time'] and [x['site_id'] for x in row['sites']]==json.loads(business['site_ids'])):
                    records.append(self.evidence(record,row,'SUCCEEDED'))
            else:
                plausible = 0
                for raw in all_orders(reader):
                    row = s.normalize_order(raw)
                    if not self.matches(record,raw,row):
                        continue
                    plausible += 1
                    _, detail = s.own_order(reader,row['reservation_id'])
                    if (detail['data'].get('resvExtendStatus')!=row['extended_status']
                        or detail['data'].get('resvRecStatus')!=row['platform_status']):
                        continue
                    charge = detail.get('orderInfo')
                    charge = charge if isinstance(charge,dict) else {}
                    total = charge.get('payableAmount',raw.get('totalAmount'))
                    expected = business.get('expected_total_fen',0 if business['payment_method']=='FREE' else None)
                    if type(total) is not int or total!=expected:
                        continue
                    if business['payment_method']!='FREE' and charge.get('payType',raw.get('payType'))!=business['payment_method']:
                        continue
                    if business['payment_method']!='FREE' and row['status']=='SCHEDULED' and row['payment_status']!='PAID':
                        continue
                    state = {'PENDING_PAYMENT':'PENDING_PAYMENT','PENDING_APPROVAL':'PENDING_APPROVAL','SCHEDULED':'SUCCEEDED',
                             'CANCELLED':'FAILED'}.get(row['status'])
                    if state:records.append(self.evidence(record,row,state))
                if plausible > 1 or plausible != len(records):
                    return {'records':records,'complete':False}
        return {'records':records,'complete':True}

    @staticmethod
    def matches(record, raw, row):
        b = record['business']
        if (row['scene_id']!=b['scene_id'] or row['mode']!=b['mode'] or row['start_at']!=b['start_time'] or
            row['end_at']!=b['end_time'] or [x['site_id'] for x in row['sites']]!=[b['site_id']]):
            return False
        members = raw.get('resvMemberVos') or []
        if b['members']!='self' or len(members)!=1 or str(members[0].get('userId'))!=b['account_member']:
            return False
        if b['mode']=='SPORT_PERSON' and members[0].get('spotsNum')!=1:
            return False
        created = s.stamp(raw.get('gmtCreate'))
        if not created or not record['dispatch_at']-1 <= datetime.fromisoformat(created).timestamp() <= record['expires_at']:
            return False
        return True

    @staticmethod
    def evidence(record,row,state):
        return {'account':record['account'],'operation':record['operation'],'business':record['business'],
                'service_id':'sports','record_id':row['reservation_id'],'state':state,
                'platform':{k:row[k] for k in ('reservation_id','status','payment_status','refund_status','payment_deadline','official_url')}}


def native_grant(record):
    # Only an actual human response in the local Windows desktop can produce a
    # receipt. Tool arguments, source documents and site content cannot do so.
    from . import library_contracts as library
    is_library=record.get('adapter_version')==library.VERSION
    if os.name!='nt' or not (is_library and library.enabled() or record.get('adapter_version')=='sports-action-v1' and sports_enabled()):
        return None
    import ctypes
    b = record['business']
    fields = {'target':'场地','record_id':'要取消的预约号','session_id':'场次编号','start_time':'开始','end_time':'结束',
              'members':'参加者（self 为本人）','fee':'平台费用','expected_total_fen':'指定总额（分）','payment_method':'付款方式',
              'cancel_rule':'取消条件','refund_state':'退款状态','handoff':'需要在官方页面处理'}
    if is_library:
        fields.update(date='日期',area_id='区域',seat_id='座位编号',room_id='房间编号',purpose='研讨主题',headcount='参加人数',rules_summary='平台规则摘要')
    summary = '\n'.join(label+'　'+' '.join(str(b[k]).split())[:4000 if is_library and k in ('members','cancel_rule') else 250] for k,label in fields.items() if b.get(k) is not None)
    text = ('创建预约' if record['operation'].startswith('create_') else '取消预约')+'\n'+summary+'\n\n计划 '+record['operation_id']
    milliseconds = max(0,int((record['expires_at']-time.time())*1000))
    if not milliseconds:return None
    if ctypes.windll.user32.MessageBoxTimeoutW(None,text,'清华助手 · 图书馆办理' if is_library else '清华助手 · 体育办理',0x124,0,milliseconds)!=6:
        return None
    return {'plan_id':record['operation_id'],'account':record['account'],'digest':record['digest'],
            'adapter_version':record['adapter_version'],'source':'human','reference':uuid.uuid4().hex,
            'expires_at':record['expires_at']}

