"""Library seat and study-room actions. One preview binds one exact transaction."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import time
from urllib.parse import urlsplit
from . import spaces as s, library_wire as wire, library_contracts as c, precheck
from .actions import digest, NotDispatched
from .bridge import CampusError

SEAT_TERMINAL = {'用户取消','已取消','系统取消','已使用','已结束','已完成','已违约'}
SEAT_CANCELLED = {'用户取消','已取消','系统取消'}
PERSONAL_SEAT = s.SEAT+'/user/index/book'
PERSONAL_ROOM = s.ROOM+'/'
LOGIN = re.compile(r'[A-Za-z0-9_-]{2,80}')


@dataclass
class Proposal:
    business: dict
    path: str
    body: dict

    @property
    def requests(self):
        if self.business['handoff']:return []
        return [wire.specification(self.business['system'],self.path,self.body)]


def interval(args):
    a,b = s.times(args)
    day = s.day(args['date'])
    start = datetime.fromisoformat(str(day)+'T'+a).replace(tzinfo=s.TZ)
    end = datetime.fromisoformat(str(day)+'T'+b).replace(tzinfo=s.TZ)
    return start,end


def enforce_future(args):
    start,end=interval(args)
    s.require(start>datetime.now(s.TZ),'LIBRARY_START_IN_PAST')
    return start,end


def joined_location(value):
    # The official seat history spells its exact location as area-area:seat.
    return re.sub(r'\s+','',value.replace('：',':'))


def seat_cancellation_terms(rules):
    selected=[text for text in rules if re.search('取消|撤销|违约|签到',text)]
    return '；'.join(selected)[:3500] or '当前规则未列明取消条件，须在官方页面核实。'


def seat_detail(reader, row):
    rid=s.ident(row['reservation_id'])
    value=reader.page.evaluate("""async url=>{
      const u=new URL(url);if(u.origin!==location.origin)throw Error('ORIGIN');
      const r=await fetch(u,{redirect:'error',signal:AbortSignal.timeout(18000)});
      if(!r.ok)throw Error('DETAIL');
      const doc=new DOMParser().parseFromString(await r.text(),'text/html'), out={};
      const keys={'预约编号':'number','预约状态':'state','预约空间':'location','开始时间':'start','结束时间':'end'};
      for(const x of doc.querySelectorAll('.form-group')){
        const label=x.querySelector('label')?.textContent.trim().replace(/[:：]$/,'');
        if(keys[label])out[keys[label]]=x.querySelector('.col-md-9')?.textContent.trim();
      }return out;
    }""",reader.base+'/user/index/look/id/'+rid)
    s.require(value.get('number')==row['reservation_number'] and
              value.get('state')==row['status_label'] and
              joined_location(value.get('location',''))==joined_location(row['location']) and
              s.stamp(value.get('start'))==row['start_time'] and s.stamp(value.get('end'))==row['end_time'],
              'LIBRARY_SEAT_DETAIL_MISMATCH')
    return value


def precheck_summary(block):
    """Compact, time-free precheck states persisted with one bound plan.

    ``checked_at`` is deliberately excluded: a replay of the same read must not
    change the digest, while a change of any decision axis must.
    """
    return {'version':str(block.get('version') or ''),'operation':str(block.get('operation') or ''),
            'selection_state':block['selection']['state'],'availability_state':block['availability']['state'],
            'eligibility_state':block['eligibility']['state'],'fees_state':block['fees']['state'],
            'rules_state':block['rules']['state'],'preparation_state':block['preparation_state']}


def plan_precheck(summary,item):
    """The bound resource's axes plus the read's selection/rules verdict."""
    states=precheck_summary(item)
    states['selection_state']=summary['selection']['state']
    if summary['rules']['state']!='NOT_APPLICABLE':
        states['rules_state']=summary['rules']['state']
    return states


def seat_proposal(reader, args):
    start,end=enforce_future(args)
    avail=s.read_seat(reader,'seat_availability',{k:args[k] for k in ('area_id','date','start_time','end_time')})
    if avail.get('status') not in (None,'BROWSER_REQUIRED'):
        raise CampusError(avail.get('reason') or 'SEAT_NOT_AVAILABLE')
    built=precheck.build('library','seat_availability',avail,avail.get('items') or [],
                         {k:args[k] for k in ('area_id','date','start_time','end_time')},None)
    selection=built['summary']
    selected=[x for x in avail.get('items') or [] if x['seat_id']==args['seat_id']]
    flags=[]
    block=None
    if avail.get('status')=='BROWSER_REQUIRED':
        flags.append('OFFICIAL_SEAT_SELECTION')
    else:
        block=next((x for x in built['items'] if x['resource']['resource_id']==args['seat_id']),None)
        s.require(block is not None,'SEAT_NOT_IN_SELECTED_AREA')
        # A free read row is not permission to book: TRUE stays exact, FALSE is
        # unavailable, and an unmapped/unknown state needs the official map.
        s.require(block['availability']['state']!='FULL','SEAT_UNAVAILABLE')
        if block['availability']['state']!='FREE':
            flags.append('OFFICIAL_SEAT_SELECTION')
    rows=wire.seat_history(reader)
    s.require(not any(x['status_label'] not in SEAT_TERMINAL and x['start_time']<end.isoformat() and
                      x['end_time']>start.isoformat() for x in rows),'SEAT_EXISTING_OVERLAP')
    segment=avail.get('segment_id')
    label=selected[0]['name'] if selected else ''
    if selected:
        try:view=wire.seat_map(reader,avail,args['seat_id'])
        except CampusError as error:
            if error.code not in ('SPACE_PAGE_FAILED','SEAT_MAP_UNAVAILABLE'):raise
            flags.append('OFFICIAL_SEAT_SELECTION');view=None
    else:view=None
    if view is not None:
        hits=view['seat']
        same_window=(view['area'],view['date'],view['start'],view['end'],view['segment'])==(
            args['area_id'],args['date'],args['start_time'],args['end_time'],segment)
        if not (same_window and len(hits)==1 and view['sameName']==1 and
                (hits[0]['id'],hits[0]['control'],hits[0]['area'],hits[0]['name'])==
                (args['seat_id'],args['seat_id'],args['area_id'],label)):
            flags.append('OFFICIAL_SEAT_SELECTION')
        elif hits[0]['status']!=1:raise CampusError('SEAT_UNAVAILABLE')
        s.require(view['authenticated'] and not view['restricted'],'SEAT_ACCOUNT_RESTRICTED')
    rules=avail.get('rules',[])
    if selection['rules']['state']=='MISSING':flags.append('CONFIRM_SEAT_RULES')
    path=avail['resource']['path']
    location='-'.join(path)+':'+label if label else ''
    url=avail.get('official_page_url') or s.SEAT+'/home/web/seat/area/'+args['area_id']
    business={**args,'system':'seat','target':' / '.join(path+[label or '官方座位图选择']),
              'location':location,'rules_summary':'；'.join(rules)[:1800],'cancel_rule':seat_cancellation_terms(rules),
              'official_url':url,'handoff':','.join(flags),'proof':digest([segment,rules,location]),
              'precheck':plan_precheck(selection,block) if block is not None else precheck_summary(selection),
              'prior_ids':sorted(x['reservation_id'] for x in rows if x['start_time']==start.isoformat() and x['end_time']==end.isoformat())}
    return Proposal(business,'/api.php/spaces/'+args['seat_id']+'/book',{'segment':segment or '', 'type':1})


def resolve_members(reader, members, user):
    s.require(len(members)==len(set(members)),'ROOM_DUPLICATE_MEMBER')
    s.require('self' in members and members[0]=='self','ROOM_APPLICANT_MUST_BE_FIRST')
    owner=s.ident(user.get('accNo'))
    s.require(not user.get('disabled'),'ROOM_ACCOUNT_RESTRICTED')
    own=wire.member_matches(reader,user['login'])
    verified=own['complete'] and len(own['exact'])==1 and str(own['exact'][0]['accNo'])==owner
    if verified:s.require(not own['exact'][0]['disabled'],'ROOM_ACCOUNT_RESTRICTED')
    ids=[owner]
    unresolved=not verified
    for login in members[1:]:
        if not LOGIN.fullmatch(login):
            unresolved=True
            continue
        matched=wire.member_matches(reader,login)
        if not matched['complete'] or len(matched['exact'])!=1:
            unresolved=True
            continue
        candidate=matched['exact'][0]
        s.require(not candidate['disabled'],'ROOM_MEMBER_RESTRICTED')
        mid=s.ident(candidate['accNo'])
        s.require(mid not in ids,'ROOM_DUPLICATE_MEMBER')
        ids.append(mid)
    return ids,unresolved


def room_window(start, end, room, now):
    rules=room['rules']
    before,after,grid=(rules.get(k) for k in ('earliestResvTime','latestResvTime','timeInterval'))
    if not all(type(v) is int and 0<=v<=525600 for v in (before,after,grid)) or not before or not grid:
        return False
    midnight=now.replace(hour=0,minute=0,second=0,microsecond=0)
    last=(midnight+timedelta(minutes=before,days=1) if before%1440==0 else now+timedelta(minutes=before))
    first=(midnight+timedelta(minutes=after) if after%1440==0 else now+timedelta(minutes=after))
    s.require(start>=first and start<last and end<=last,'ROOM_OUTSIDE_BOOKING_WINDOW')
    covering=[x for x in room['opening_intervals'] if x['start_time']<=start.strftime('%H:%M')<end.strftime('%H:%M')<=x['end_time']]
    s.require(bool(covering),'ROOM_NOT_OPEN')
    anchor=s.minutes(covering[0]['start_time'])
    s.require(all((s.minutes(v.strftime('%H:%M'))-anchor)%grid==0 for v in (start,end)),'ROOM_TIME_GRID_MISMATCH')
    return True


def find_room(reader, args, for_booking=True):
    selected={k:args[k] for k in ('kind_id','date','start_time','end_time','headcount') if k in args}
    if 'headcount' in selected:selected['people']=selected.pop('headcount')
    for number in range(1,51):
        out=s.read_rooms(reader,'room_availability',{**selected,'page':number,'limit':50})
        for room in out['items']:
            if room['room_id']==args['room_id']:
                if for_booking:
                    s.require(out['account_restrictions']['active_records']==0,'ROOM_ACCOUNT_RESTRICTED')
                    s.require(room['party_state']=='IN_RANGE',room['party_state'])
                    # A missing duration rule is a rule-read gap, not “no limit”:
                    # keep the room so the plan can hand off for a rule recheck.
                    s.require(room['interval_state'] in ('FREE_TIME_ONLY','RULE_UNVERIFIED'),'ROOM_'+room['interval_state'])
                    s.require(not room['only_view'],'ROOM_VIEW_ONLY')
                return room,out['resource'],out
        if not out.get('next_page'):break
    raise CampusError('ROOM_NOT_IN_SELECTED_CATEGORY')


def room_proposal(reader,args):
    start,end=enforce_future(args)
    s.require(args['purpose'].strip() and len(args['members'])==args['headcount'],'ROOM_MEMBERS_REQUIRED')
    room,kind,out=find_room(reader,args)
    built=precheck.build('library','room_availability',out,[room],
                         {k:args[k] for k in ('kind_id','date','start_time','end_time')},None)
    item=built['items'][0]
    prestates=plan_precheck(built['summary'],item)
    flags=[]
    if item['availability']['state']=='RULE_UNVERIFIED':flags.append('CONFIRM_ROOM_DURATION_RULE')
    elif item['availability']['state']!='FREE':flags.append('CONFIRM_ROOM_AVAILABILITY')
    if item['eligibility']['state']=='INELIGIBLE':
        reason=next(iter(item['eligibility']['reasons']),'ROOM_INELIGIBLE')
        raise CampusError({'PARTY_TOO_SMALL':'PARTY_TOO_SMALL','PARTY_TOO_LARGE':'PARTY_TOO_LARGE',
                           'VIEW_ONLY_RESOURCE':'ROOM_VIEW_ONLY',
                           'ACCOUNT_RESTRICTIONS_REPORTED':'ROOM_ACCOUNT_RESTRICTED'}.get(reason,'ROOM_INELIGIBLE'))
    if not room_window(start,end,room,datetime.now(s.TZ)):flags.append('CONFIRM_OPENING_RULE')
    if room['platform_open_state']!=0 or room['platform_device_status']!=0 or room['only_view'] is not False:
        flags.append('CONFIRM_ROOM_ELIGIBILITY')
    user=wire.room_identity(reader)
    ids,unresolved=resolve_members(reader,args['members'],user)
    if unresolved:flags.append('VERIFY_MEMBERS_ON_OFFICIAL_PAGE')
    if str(user.get('verification'))!='0':flags.append('OFFICIAL_VERIFICATION')
    form=user.get('form')
    if (not form or form['notice'] or str(form['memo']) not in ('0','None') or
        room['rules'].get('limit')!=0 or (str(form['theme']) not in ('0','None') and str(form['freeTitle'])!='1')):
        flags.append('CONFIRM_OFFICIAL_FORM_AND_RULES')
    if kind['class_kind']!=1:flags.append('CONFIRM_ROOM_FORM')
    rows=wire.room_history(reader,args['date'])
    for row in rows:
        norm=s.own_room_row(row)
        if norm['platform_status'] not in (128,512) and norm['start_time']<end.isoformat() and norm['end_time']>start.isoformat():
            raise CampusError('ROOM_EXISTING_OVERLAP')
    rule=room['rules'].get('cancelTime')
    if type(rule) is not int or rule<0:flags.append('CONFIRM_CANCELLATION_RULE')
    summary=f"允许 {room['min_people']} 至 {room['max_people']} 人，平台时长 {room['rules'].get('minResvTime')} 至 {room['rules'].get('maxResvTime')} 分钟。"
    business={**args,'system':'room','target':kind['name']+' / '+room['name'],
              'rules_summary':summary,'cancel_rule':cancel_terms(rule,user),
              'handoff':','.join(flags),'official_url':PERSONAL_ROOM,'resolved_members':ids,'applicant':ids[0],
              'precheck':prestates,
              'prior_ids':sorted(s.ident(row['resvId']) for row in rows if s.stamp(row.get('resvBeginTime'))==start.isoformat()
                                 and s.stamp(row.get('resvEndTime'))==end.isoformat()),
              'proof':digest([kind['class_kind'],room['rules'],room['min_people'],room['max_people'],room['reservation_notice'],
                              user.get('verification'),form,room['platform_open_state'],room['platform_device_status']])}
    body={'sysKind':kind['class_kind'],'appAccNo':int(ids[0]),'memberKind':2 if len(ids)>1 else 1,
          'resvBeginTime':start.strftime('%Y-%m-%d %H:%M:%S'),'resvEndTime':end.strftime('%Y-%m-%d %H:%M:%S'),
          'testName':args['purpose'],'resvKind':2,'resvProperty':0,'appUrl':'','resvMember':[int(v) for v in ids],
          'resvDev':[int(args['room_id'])],'memo':'','captcha':'','addServices':[]}
    return Proposal(business,'/ic-web/reserve',{} if unresolved else body)


def stable_room_members(raw):
    members=raw.get('resvMemberInfoList')
    if not isinstance(members,list) or not members:return None
    ids=[str(x.get('accNo') or '') for x in members if isinstance(x,dict)]
    if len(ids)!=len(members) or len(set(ids))!=len(ids) or not all(re.fullmatch(s.NUM,x) for x in ids):return None
    return sorted(ids)


def cancel_terms(cutoff,user):
    if type(cutoff) is not int or not 0<=cutoff<=525600:return '在官方页面核实取消期限'
    reduction=(user.get('form') or {}).get('cancellationReduction') or 0
    if isinstance(reduction,str) and reduction.isdigit():reduction=int(reduction)
    if type(reduction) is not int or not 0<=reduction<=cutoff:return '平台规则提前 '+str(cutoff)+' 分钟，页面显示期限须在官方确认'
    return f'平台规则提前 {cutoff} 分钟。页面显示提前 {cutoff-reduction} 分钟。'


def current_room_rule(reader, raw, row, date):
    s.require(len(row['rooms'])==1,'ROOM_RECORD_RESOURCE_UNVERIFIED')
    target=row['rooms'][0]['room_id']
    kinds=s.room_types(reader.call('/ic-web/roomMenu'))
    hinted=(raw.get('resvDevInfoList') or [{}])[0].get('kindId')
    if hinted:kinds=[k for k in kinds if k['kind_id']==str(hinted)]
    for kind in kinds:
        if kind['class_kind']!=1:continue
        args={'kind_id':kind['kind_id'],'room_id':target,'date':date}
        try:room,_,_=find_room(reader,args,for_booking=False)
        except CampusError as error:
            if error.code=='ROOM_NOT_IN_SELECTED_CATEGORY':continue
            raise
        return room['rules']
    raise CampusError('ROOM_RECORD_RESOURCE_UNVERIFIED')


def cancel_proposal(reader,args):
    flags=[]
    if args['system']=='seat':
        matches=[x for x in wire.seat_history(reader) if x['reservation_id']==args['record_id']]
        s.require(len(matches)==1,'SEAT_OWN_RECORD_NOT_FOUND')
        row=matches[0]
        seat_detail(reader,row)
        s.require(row['start_time'][:10]==args['date'],'LIBRARY_RECORD_DATE_MISMATCH')
        s.require(row['cancel_control_visible'] and row['status_label'] not in SEAT_TERMINAL,'SEAT_CANCELLATION_UNAVAILABLE')
        areas,_=s.seat_catalog(reader,include_inactive=True)
        location=joined_location(row['location']).rsplit(':',1)[0]
        matching=[x for x in areas if x['leaf'] and joined_location('-'.join(x['path']))==location]
        rules=[]
        if len(matching)==1:
            reader.goto('/home/web/seat/area/'+matching[0]['area_id'])
            rules=[s.plain(x) for x in reader.page.evaluate("Array.isArray(window.ska?.bookRule)?window.ska.bookRule:[]") if s.plain(x)]
        else:flags.append('CONFIRM_ORIGINAL_AREA')
        if not any('取消' in rule or '撤销' in rule for rule in rules):flags.append('CONFIRM_SEAT_CANCELLATION_RULE')
        business={**args,'target':row['location'],'location':row['location'],'start_time':row['start_time'],'end_time':row['end_time'],
                  'original_state':row['status_label'],'rules_summary':'；'.join(rules)[:1800],
                  'cancel_rule':seat_cancellation_terms(rules),'official_url':PERSONAL_SEAT,
                  'handoff':','.join(flags),'proof':digest([rules,row['reservation_number'],row['status_label']])}
        return Proposal(business,'/api.php/profile/books/'+args['record_id'],{'_method':'delete','id':args['record_id'],'operateChannel':2})
    rows=wire.room_history(reader,args['date'])
    matches=[row for row in rows if s.ident(row.get('resvId'))==args['record_id']]
    s.require(len(matches)==1,'ROOM_OWN_RECORD_NOT_FOUND')
    raw=matches[0];row=s.own_room_row(raw);user=wire.room_identity(reader)
    s.require(str(raw.get('appAccNo'))==s.ident(user.get('accNo')),'ROOM_APPLICANT_MISMATCH')
    s.require(row['start_time'][:10]==args['date'],'LIBRARY_RECORD_DATE_MISMATCH')
    s.require(row['platform_status']>0 and not row['platform_status'] & (4|128|512) and datetime.fromisoformat(row['start_time'])>datetime.now(s.TZ),
              'ROOM_CANCELLATION_UNAVAILABLE')
    rid=raw.get('uuid')
    s.require(isinstance(rid,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',rid),'ROOM_RECORD_UUID_UNAVAILABLE')
    members=stable_room_members(raw)
    if not members:flags.append('VERIFY_MEMBERS_ON_OFFICIAL_PAGE')
    # The selected device's live rule is required; the list can omit it.
    rules=current_room_rule(reader,raw,row,args['date'])
    cutoff=rules.get('cancelTime')
    if type(cutoff) is not int or not 0<=cutoff<=525600:flags.append('CONFIRM_CANCELLATION_RULE')
    else:s.require(datetime.now(s.TZ)<datetime.fromisoformat(row['start_time'])-timedelta(minutes=cutoff),'ROOM_CANCELLATION_WINDOW_CLOSED')
    locations=[x['room_id'] for x in row['rooms']]
    s.require(bool(locations),'ROOM_RECORD_LOCATION_UNKNOWN')
    business={**args,'target':'、'.join(x['name'] for x in row['rooms']),'location':','.join(locations),
              'start_time':row['start_time'],'end_time':row['end_time'],'record_uuid':rid,'original_state':str(row['platform_status']),
              'applicant':str(raw['appAccNo']),'resolved_members':members or [],'handoff':','.join(flags),'official_url':PERSONAL_ROOM,
              'cancel_rule':cancel_terms(cutoff,user),
              'rules_summary':'取消这笔预约会影响其全部参加者。','proof':digest([rid,rules,members,row['platform_status']])}
    return Proposal(business,'/ic-web/reserve/delete',{'uuid':rid})


def room_state(mask):
    return {1:'SUCCEEDED',2:'SUCCEEDED',4:'SUCCEEDED',8:'PENDING_PAYMENT',256:'PENDING_APPROVAL',
            8192:'PENDING_APPROVAL',512:'FAILED'}.get(mask)


def reconcile(reader,record,op):
    b=record['business']
    system=b['system']
    rid=b.get('record_id')
    records=[]
    uncertain=False
    if system=='seat':
        rows=wire.seat_history(reader)
        for row in rows:
            if op==c.CANCEL and row['reservation_id']!=rid:continue
            if not b.get('location') or joined_location(row['location'])!=joined_location(b['location']):continue
            if op==c.CANCEL:
                if (row['start_time'],row['end_time'])!=(b['start_time'],b['end_time']):continue
                state='SUCCEEDED' if row['status_label'] in SEAT_CANCELLED else None
            else:
                start,end=interval(b)
                if row['reservation_id'] in b['prior_ids'] or (row['start_time'],row['end_time'])!=(start.isoformat(),end.isoformat()):continue
                if time.time()>record['expires_at'] and record.get('record_id')!=row['reservation_id']:
                    uncertain=True;continue
                state={'已预约':'SUCCEEDED','预约成功':'SUCCEEDED','待签到':'SUCCEEDED','待审核':'PENDING_APPROVAL',
                       '审核中':'PENDING_APPROVAL','用户取消':'FAILED','已取消':'FAILED'}.get(row['status_label'])
            if not state:
                uncertain=True
                continue
            seat_detail(reader,row)
            records.append((row['reservation_id'],state,row['status_label']))
    else:
        rows=wire.room_history(reader,b['date'])
        for raw in rows:
            row=s.own_room_row(raw)
            ids=[x['room_id'] for x in row['rooms']]
            if op==c.CANCEL:
                if row['reservation_id']!=rid:continue
                # The platform normally removes cancelled room records. A row
                # still present is not proof of a cancellation.
                uncertain=True
                continue
            start,end=interval(b)
            if row['reservation_id'] in b['prior_ids'] or ids!=[b['room_id']] or (row['start_time'],row['end_time'])!=(start.isoformat(),end.isoformat()):continue
            created=raw.get('gmtCreate') or raw.get('resvCreateTime')
            if created:
                created_at=datetime.fromisoformat(s.stamp(created)).timestamp()
                if not record['dispatch_at']-1<=created_at<=record['expires_at']:uncertain=True;continue
            elif time.time()>record['expires_at'] and record.get('record_id')!=row['reservation_id']:
                uncertain=True;continue
            if (len(b['resolved_members'])!=b['headcount'] or stable_room_members(raw)!=sorted(b['resolved_members']) or
                str(raw.get('appAccNo'))!=b['applicant'] or raw.get('testName')!=b['purpose']):
                uncertain=True
                continue
            state=room_state(row['platform_status'])
            if not state:uncertain=True;continue
            records.append((row['reservation_id'],state,row['platform_status']))
    if op==c.CANCEL and record.get('platform_accepted') is True:
        matching = any((x.get('reservation_id') if system=='seat' else str(x.get('resvId')))==rid for x in rows)
        if not matching:
            records.append((rid,'SUCCEEDED','ACCEPTED_AND_ABSENT_FROM_COMPLETE_OWN_LIST'))
    # No-op readbacks never turn an empty list into success.
    evidence=[{'service_id':'library','account':record['account'],'business':b,'operation':op,'record_id':key,'state':state,
               'platform':{'reservation_id':key,'system':system,'platform_status':label,'official_url':b['official_url']}}
              for key,state,label in records]
    return {'complete':not uncertain,'records':evidence}


class LibraryAction:
    version=c.VERSION
    def __init__(self,browsers,op):
        self.browsers,self.op=browsers,op

    def system(self,business):
        return business['system'] if self.op==c.CANCEL else 'seat' if self.op==c.CREATE_SEAT else 'room'

    def proposal(self,reader,business):
        args={k:business[k] for k in c.INPUTS[self.op]}
        if self.op==c.CREATE_SEAT:return seat_proposal(reader,args)
        if self.op==c.CREATE_ROOM:return room_proposal(reader,args)
        return cancel_proposal(reader,args)

    def preview(self,args):
        with s.Reader(self.browsers,self.system(args)) as reader:
            return self.proposal(reader,args).business

    def bound(self,reader,record):
        s.require(self.browsers.sessions[reader.sid]['account_id']==record['account'],'ACTION_ACCOUNT_MISMATCH')

    def preflight(self,record):
        s.require(c.enabled(),'LIBRARY_WRITES_DISABLED')
        with s.Reader(self.browsers,self.system(record['business'])) as reader:
            self.bound(reader,record)
            p=self.proposal(reader,record['business'])
        return {'business':p.business,'context':None,'official_handoff':p.business['handoff'] or None,
                'checks':{'availability':True,'eligibility':not bool(p.business['handoff']),
                          'rules':not bool(p.business['handoff']),'fees':True},'requests':p.requests}

    def dispatch(self,record,gate):
        system=self.system(record['business'])
        with s.Reader(self.browsers,system) as reader:
            self.bound(reader,record)
            p=self.proposal(reader,record['business'])
            if (not c.enabled() or p.business!=record['business'] or p.business['handoff'] or
                    time.time()>=min(record['expires_at'],record['authorization']['evidence']['expires_at'])):
                raise NotDispatched()
            return wire.dispatch_exact(reader,system,p.path,p.body,gate)

    def reconcile(self,record):
        with s.Reader(self.browsers,self.system(record['business'])) as reader:
            self.bound(reader,record)
            return reconcile(reader,record,self.op)

