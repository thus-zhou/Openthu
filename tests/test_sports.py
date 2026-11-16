"""Fixed official shapes and synthetic transactions. Never submit a campus order."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import pytest

from thu_assistant import sports as s, sports_actions as a, sports_transport as net
from thu_assistant.actions import ActionEngine, RequestGate, RequestSpec
from thu_assistant.action_store import ActionStore
from thu_assistant.bridge import CampusError
from thu_assistant.queries import describe
from thu_assistant.todos import normalize
from thu_assistant.browser import read_request

DAY='2099-02-03'
ARGS={'scene_id':'scene','room_id':'room','site_id':'court','session_id':'slot','date':DAY,
      'mode':'SPORT_GROUP','members':'self','payment_method':'FREE'}
SLOT={'uuid':'slot','beginDate':20990203,'endDate':20990203,'beginTime':'09:00','endTime':'10:00',
      'sceneUseType':'SPORT_GROUP','reserveStatus':{'reserveStatus':'Y'},'userFeeDetails':{'chargingUnitPrice':0,'payType':3}}
FIELD={'uuid':'court','sceneUuid':'scene','siteName':'合成场地','siteType':'DEV','sessionVo':[SLOT],
       'formRuleVo':None,'reserveRule':{'cancelBeforeStart':10}}
ROW={'resvUuid':'rec','sceneUuid':'scene','sceneName':'合成项目','resvTime':{'startTime':DAY+' 09:00:00','endTime':DAY+' 10:00:00'},
     'siteVoList':[{'uuid':'court','siteName':'合成场地'}],'resvStatus':'RESV_UNEFFECT','resvExtendStatus':1,'resvProperty':0,
     'resvCheckStatus':'RESV_APPROVE_PASS','sceneUseType':'SPORT_GROUP','orderStatus':4,'paidAmount':0,
     'orderRefundStatus':0,'cancelBeforeStartTime':10,'resvMemberVos':[{'userId':'selfid'}]}
AUTH='为这个合成测试批准指定的一笔业务'


class Fake:
    def __init__(self):
        self.calls=[]
        self.scenes=[{'uuid':'scene','sceneName':'北体兵乓球','relatedType':'DEV'}]
        self.fields=[deepcopy(FIELD)]
        self.rows=[]
        self.captcha='0'
        self.detail_amount=0
        self.session={'account_id':'test-account'}
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def call(self,path,params=None):
        params=params or {}
        self.calls.append((path,deepcopy(params)))
        if path==s.SCENE:return {'data':self.scenes}
        if path==s.LOCATION:
            return {'data':[{'uuid':params['siteType'].lower(),'siteType':params['siteType'],'siteName':'合成地点'}]}
        if path==s.PAGE:
            rows=[f for f in self.fields if f['sessionVo'][0]['sceneUseType']==params['sceneUseType']]
            return {'count':len(rows),'data':rows if params['pageNum']==1 else []}
        if path==s.RECORDS:
            start=(params['pageNum']-1)*params['pageSize']
            return {'count':len(self.rows),'data':self.rows[start:start+params['pageSize']]}
        if path==s.DETAIL:
            row=next(r for r in self.rows if r['resvUuid']==params['resvUuid'])
            return {'data':{'resvUuid':params['resvUuid'],'data':{'moduleData':{},'resvExtendStatus':row['resvExtendStatus'],
                    'resvRecStatus':row['resvStatus']},'orderInfo':{'payableAmount':self.detail_amount,'payType':'PAY_ONLINE'}}}
        if path==net.USER:return {'data':{'id':'selfid'}}
        if path==net.CAPTCHA:return {'data':{'sysKey':'RESV_CODE','sysValue':self.captcha}}
        if path==net.FORM_BRIEF+'official-form':return {'data':{'deployUuid':'synthetic-deployment'}}
        raise AssertionError(path)


def test_catalog_and_ambiguity_does_not_choose_first_scene():
    r=Fake()
    r.scenes.append({'uuid':'other','sceneName':'另一个乒乓球','relatedType':'DEV'})
    out=s.availability(r,{'name':'乒乓球'})
    assert out['reason']=='SELECT_EXACT_SCENE' and len(out['candidates'])==2
    assert [x[0] for x in r.calls]==[s.SCENE]


def test_availability_resolves_room_and_native_calendar_integer():
    r=Fake()
    out=s.availability(r,{'scene_id':'scene','date':DAY})
    assert out['complete'] and out['mode']=='SPORT_GROUP'
    slot=out['items'][0]['sessions'][0]
    assert slot['start_at']==DAY+'T09:00:00+08:00' and slot['available'] is True
    assert slot['unit_fee_fen']==0
    assert [p['siteType'] for path,p in r.calls if path==s.LOCATION]==['CAMPUS','BUILDING','FLOOR','ROOM']
    req=next(p for path,p in r.calls if path==s.PAGE)
    assert req['classTypeUuid']=='room' and req['classTypeEnum']=='ROOM'


def test_wrong_room_or_unknown_mode_never_reports_sold_out():
    r=Fake()
    assert s.availability(r,{'scene_id':'scene','room_id':'foreign','date':DAY})['reason']=='SELECT_EXACT_ROOM'
    r.fields=[]
    out=s.availability(r,{'scene_id':'scene','date':DAY})
    assert not out['complete'] and out['checked_modes']==list(s.MODES)


def test_person_mode_and_locked_slot_preserve_platform_reason():
    r=Fake()
    slot=r.fields[0]['sessionVo'][0]
    slot.update(sceneUseType='SPORT_PERSON',reserveStatus={'reserveStatus':'N','reserveStatusReason':'场次已锁场'})
    out=s.availability(r,{'scene_id':'scene','date':DAY})
    assert out['mode']=='SPORT_PERSON'
    result=out['items'][0]['sessions'][0]
    assert result['available'] is False and result['reason']=='场次已锁场'


def test_free_period_and_unknown_dates_do_not_invent_slots():
    f={**FIELD,'sessionVo':[],'supportPeriod':'Y','reserveStatus':{'availableRange':[{'startTime':'22:00'}]}}
    row=s.field_row(f,'scene','SPORT_GROUP',DAY,'room')
    assert row['free_period_requires_official_page'] and row['sessions']==[]
    with pytest.raises(CampusError,match='DATE_MISMATCH'):
        s.field_row(FIELD,'scene','SPORT_GROUP','2099-02-04','room')


@pytest.mark.parametrize('rows,total', [([],1),([ROW,ROW],2),([ROW],0)])
def test_bad_page_counts_and_duplicates_fail_closed(rows,total):
    with pytest.raises(CampusError):s.page_rows({'data':rows,'count':total},1,50,'resvUuid')


@pytest.mark.parametrize('flags,phase,approval,payment,state',[
    (2,'RESV_END','RESV_APPROVE_PASS',4,'CANCELLED'),
    (1,'RESV_END','RESV_APPROVE_PASS',4,'COMPLETED'),
    (1,'RESV_UNEFFECT','RESV_WAIT_APPROVE',4,'PENDING_APPROVAL'),
    (1,'RESV_EFFECT','RESV_APPROVE_PASS',1,'PENDING_PAYMENT'),
    (1,'RESV_EFFECT','RESV_APPROVE_PASS',4,'SCHEDULED'),
    (1,'new','new',99,'UNKNOWN')])
def test_separate_reservation_payment_and_refund(flags,phase,approval,payment,state):
    row=s.normalize_order({**ROW,'resvExtendStatus':flags,'resvStatus':phase,'resvCheckStatus':approval,
                           'orderStatus':payment,'orderRefundStatus':4})
    assert row['status']==state and row['refund_status']=='REFUNDING'
    event=normalize('sports.orders',row,'2026-11-06T12:00:00+08:00')
    assert event['status']==state and event['due_at'] is None and event['source_url']==net.PERSONAL


def test_detail_only_for_owner_and_bound_id():
    r=Fake();r.rows=[ROW]
    with pytest.raises(CampusError,match='OWN_RECORD_NOT_FOUND'):s.own_order(r,'foreign')
    assert all(path!=s.DETAIL for path,_ in r.calls)
    row,detail=s.own_order(r,'rec')
    assert row['resvUuid']==detail['resvUuid']


@pytest.mark.parametrize('status,payload,expected',[
    (200,{'code':500,'message':'维护中'},'MAINTENANCE'),
    (403,{},'FORBIDDEN'),(401,{},'AUTH_REQUIRED'),
    (200,{'code':500,'message':'请求频繁'},'RATE_LIMITED'),
    (200,{'code':'0','data':[]},'SPORTS_RESPONSE_INVALID')])
def test_errors_do_not_look_like_empty_orders(status,payload,expected):
    with pytest.raises(CampusError,match=expected):
        net.checked_response({'status':status,'payload':payload,'retry':None})


def test_retry_after_and_backoff_only_for_reads(monkeypatch):
    delays=[]
    monkeypatch.setattr(net,'wait_or_cancel',lambda seconds:delays.append(seconds))
    calls=[]
    def evaluate(*args):
        calls.append(1)
        return {'status':429,'retry':'5' if len(calls)==1 else None,'payload':{}}
    r=net.Reader(None);r.page=SimpleNamespace(evaluate=evaluate)
    with pytest.raises(CampusError,match='RATE_LIMITED'):r.call(s.SCENE)
    assert len(calls)==4 and [x for x in delays if x>.25]==[5,4,8]
    with pytest.raises(CampusError,match='UNREGISTERED_SPORTS_READ'):r.call(net.CREATE)
    assert len(calls)==4


def test_long_retry_after_and_maintenance_stop(monkeypatch):
    monkeypatch.setattr(net,'wait_or_cancel',lambda seconds:None)
    r=net.Reader(None)
    r.page=SimpleNamespace(evaluate=lambda *args:{'status':429,'retry':'600','payload':{}})
    with pytest.raises(CampusError) as exc:r.call(s.SCENE)
    assert exc.value.retry_after_ms==600000
    r.page=SimpleNamespace(evaluate=lambda *args:{'status':200,'payload':{'code':500,'message':'维护中'}})
    with pytest.raises(CampusError,match='MAINTENANCE'):r.call(s.SCENE)


def test_read_gate_blocks_writes_and_unknown_parameters():
    prefix=net.ORIGIN+net.PREFIX
    assert read_request(prefix+s.RECORDS,'POST','{"pageNum":1}')
    assert read_request(prefix+s.DETAIL+'?resvUuid=test','GET')
    assert not read_request(prefix+s.RECORDS,'POST','{"unexpected":1}')
    for method in ['GET','POST']:
        assert not read_request(prefix+net.CREATE,method,'{}')
        assert not read_request(prefix+net.CANCEL,method,'{}')


def test_preview_pricing_members_platform_form_and_official_gate():
    f=Fake();f.captcha='1'
    f.fields[0]['formRuleVo']={'formUuid':'official-form'}
    row,body=a.creation_preview(f,ARGS)
    assert set(row['handoff'].split(','))=={'OFFICIAL_CAPTCHA','OFFICIAL_FORM','CONFIRM_TOTAL_AND_PAYMENT_METHOD'}
    assert row['site_id']=='court' and row['start_time'].startswith(DAY) and row['fee'].endswith('0.00 元')
    assert body['siteSessionReserve'][0]['sessionDetailUuid']=='slot' and body['resvMember']==['selfid']
    assert net.CREATE not in [x[0] for x in f.calls]


def test_unpaid_existing_slot_or_changed_fee_blocks_prepare():
    f=Fake();f.rows=[{**ROW,'orderStatus':1}]
    with pytest.raises(CampusError,match='UNPAID'):a.creation_preview(f,ARGS)
    f.rows=[ROW]
    with pytest.raises(CampusError,match='EXISTING_MATCHING'):a.creation_preview(f,ARGS)
    f.rows=[];f.fields[0]['sessionVo'][0]['userFeeDetails']['chargingUnitPrice']=300
    with pytest.raises(CampusError,match='FEE_CHANGED'):a.creation_preview(f,ARGS)


def make_engine(tmp_path,monkeypatch,f,op='create_reservation'):
    import thu_assistant.sports_actions as actions
    monkeypatch.setattr(actions,'Reader',lambda browsers:f)
    adapter=a.SportsAction(None,net.CREATE if op=='create_reservation' else net.CANCEL)
    def consent(r):
        return {'plan_id':r['operation_id'],'account':r['account'],'digest':r['digest'],'adapter_version':r['adapter_version'],
                'source':'human','reference':'synthetic-local-human','expires_at':r['expires_at']}
    engine=ActionEngine(store=ActionStore(tmp_path),adapters={('sports',op):adapter},
                        account_provider=lambda:'test-account',consent_verifier=consent)
    return engine,adapter


def test_missing_fields_and_spoofed_output(tmp_path,monkeypatch):
    f=Fake();e,_=make_engine(tmp_path,monkeypatch,f)
    r=e.prepare('sports','create_reservation',{'scene_id':'scene'})
    assert r['status']=='NEEDS_USER_ACTION' and 'session_id' in r['missing_fields'] and not f.calls
    with pytest.raises(CampusError,match='INVALID_ARGUMENTS'):
        e.prepare('sports','create_reservation',{**ARGS,'handoff':''})


def test_price_or_platform_mode_changes_after_preview(tmp_path,monkeypatch):
    f=Fake();e,_=make_engine(tmp_path,monkeypatch,f)
    r=e.prepare('sports','create_reservation',ARGS)
    f.captcha='1'
    x=e.execute(r['plan_id'],AUTH)
    assert x['status']=='FAILED' and x['reason']=='PRECONDITIONS_CHANGED_PREPARE_AGAIN'


def test_handoff_resumes_readonly_then_matches_paid_or_free(tmp_path,monkeypatch):
    f=Fake();f.captcha='1';e,_=make_engine(tmp_path,monkeypatch,f)
    p=e.prepare('sports','create_reservation',ARGS)
    r=e.execute(p['plan_id'],AUTH)
    assert r['status']=='AWAITING_OFFICIAL' and r['official_url'].endswith('#/sports')
    assert e.execute(p['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL'
    f.rows=[{**ROW,'gmtCreate':datetime.now(s.TZ).isoformat()}]
    actual=e.result(p['plan_id'])
    assert actual['status']=='SUCCEEDED' and actual['platform']['reservation_id']=='rec'
    assert all(path not in (net.CREATE,net.CANCEL) for path,_ in f.calls)


@pytest.mark.parametrize('duplicates,expected_total,expected',[(False,300,'PENDING_PAYMENT'),(True,300,'UNKNOWN'),
                                                               (False,400,'UNKNOWN')])
def test_pending_payment_not_paid_ambiguous_or_wrong_total(tmp_path,monkeypatch,duplicates,expected_total,expected):
    f=Fake();f.fields[0]['sessionVo'][0]['userFeeDetails']['chargingUnitPrice']=300
    e,_=make_engine(tmp_path,monkeypatch,f)
    p=e.prepare('sports','create_reservation',{**ARGS,'expected_total_fen':expected_total,'payment_method':'PAY_ONLINE'})
    assert e.execute(p['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL'
    f.rows=[{**ROW,'gmtCreate':datetime.now(s.TZ).isoformat(),'orderStatus':1}]
    if duplicates:f.rows.append({**f.rows[0],'resvUuid':'second'})
    f.detail_amount=300
    r=e.result(p['plan_id'])
    assert r['status']==expected
    if expected=='PENDING_PAYMENT':assert r['platform']['payment_status']=='PENDING_PAYMENT'


def test_cancel_readback_after_lost_response_does_not_replay(tmp_path,monkeypatch):
    f=Fake();f.rows=[deepcopy(ROW)]
    e,adapter=make_engine(tmp_path,monkeypatch,f,'cancel_reservation')
    sent=[]
    def send(reader,path,body,gate):
        assert gate.consume(net.ORIGIN+net.PREFIX+net.CANCEL,'POST',json.dumps(body))
        sent.append(1);f.rows[0]['resvExtendStatus']=2
        raise TimeoutError()
    monkeypatch.setattr(a,'dispatch_exact',send)
    p=e.prepare('sports','cancel_reservation',{'record_id':'rec'})
    assert p['preview']['record_id']=='rec'
    r=e.execute(p['plan_id'],AUTH)
    assert r['status']=='SUCCEEDED' and r['platform']['refund_status']=='NONE'
    assert e.execute(p['plan_id'],AUTH)['status']=='SUCCEEDED' and len(sent)==1


def test_failed_cancel_never_infers_refund_or_retries(tmp_path,monkeypatch):
    f=Fake();f.rows=[deepcopy(ROW)]
    e,adapter=make_engine(tmp_path,monkeypatch,f,'cancel_reservation')
    sent=[]
    def send(reader,path,body,gate):
        sent.append(1);assert gate.consume(net.ORIGIN+net.PREFIX+net.CANCEL,'POST',json.dumps(body))
    monkeypatch.setattr(a,'dispatch_exact',send)
    p=e.prepare('sports','cancel_reservation',{'record_id':'rec'})
    assert e.execute(p['plan_id'],AUTH)['status']=='UNKNOWN'
    assert e.execute(p['plan_id'],AUTH)['status']=='UNKNOWN' and len(sent)==1


def test_write_switch_preserves_orders_and_reconciliation(tmp_path,monkeypatch):
    f=Fake();e,_=make_engine(tmp_path,monkeypatch,f)
    p=e.prepare('sports','create_reservation',ARGS)
    monkeypatch.setenv('THU_SPORTS_WRITE','0')
    with pytest.raises(CampusError,match='SPORTS_WRITES_DISABLED'):e.execute(p['plan_id'],AUTH)
    d=describe('sports')
    assert 'orders' in [x['operation'] for x in d['operations']]
    assert not any(x['enabled'] for x in d['actions'])


@pytest.mark.parametrize('case,expected', [('unique',None),('duplicate','SPORTS_DETAIL_AMBIGUOUS'),('other','SPORTS_DETAIL_MISMATCH')])
def test_detail_without_uuid_requires_unique_independent_match(case,expected):
    r=Fake();r.rows=[deepcopy(ROW)]
    if case=='duplicate':r.rows.append({**ROW,'resvUuid':'rec2'})
    native=r.call
    def call(path,params=None):
        if path!=s.DETAIL:return native(path,params)
        data={'resvMember':[{'userId':'selfid'}],'resvSite':[{'sceneUuid':'','uuid':'other' if case=='other' else 'court'}],
              'resvTime':[ROW['resvTime']]}
        return {'data':{'resvUuid':'','data':{'moduleData':data,'resvExtendStatus':1,'resvRecStatus':'RESV_EFFECT'}}}
    r.call=call
    if expected:
        with pytest.raises(CampusError,match=expected):s.own_order(r,'rec')
    else:assert s.own_order(r,'rec')[0]['resvUuid']=='rec'


def test_direct_free_order_response_lost_requires_remote_readback(tmp_path,monkeypatch):
    f=Fake()
    f.fields[0]['sessionVo'][0]['userFeeDetails']['payableAmount']=0
    e,_=make_engine(tmp_path,monkeypatch,f)
    sent=[]
    def send(reader,path,body,gate):
        assert gate.consume(net.ORIGIN+net.PREFIX+net.CREATE,'POST',json.dumps(body))
        sent.append(1)
        f.rows=[{**ROW,'gmtCreate':datetime.now(s.TZ).isoformat()}]
        raise TimeoutError()
    monkeypatch.setattr(a,'dispatch_exact',send)
    p=e.prepare('sports','create_reservation',ARGS)
    assert p['preview']['handoff']==''
    assert e.execute(p['plan_id'],AUTH)['platform']['reservation_id']=='rec'
    assert e.execute(p['plan_id'],AUTH)['status']=='SUCCEEDED' and sent==[1]


@pytest.mark.parametrize('change', ['body','method','frame','valid'])
def test_signed_request_bound_to_body_method_and_frame(change):
    path=net.CANCEL
    unsigned=net.ORIGIN+net.PREFIX+path
    signed=unsigned+'?appId=1&nonce=n&sign=s&timeStamp=123'
    body={'resvUuid':'rec'}
    gate=RequestGate([RequestSpec(unsigned,'POST',json.dumps(body))])
    class Page:
        main_frame=object()
        def route(self,regex,guard):
            assert regex.fullmatch(signed)
            self.guard=guard
        def unroute(self,*args):self.removed=True
        def evaluate(self,script,args=None):
            if args is not None:return {'url':signed,'body':json.dumps(body)}
            req=SimpleNamespace(url=signed,frame=object() if change=='frame' else self.main_frame,
                                method='GET' if change=='method' else 'POST',
                                post_data=json.dumps({'resvUuid':'altered'} if change=='body' else body))
            self.guard(SimpleNamespace(request=req,continue_=lambda:None,abort=lambda:None))
    p=Page()
    if change=='valid':a.dispatch_exact(SimpleNamespace(page=p),path,body,gate)
    else:
        with pytest.raises(a.NotDispatched):a.dispatch_exact(SimpleNamespace(page=p),path,body,gate)
    assert p.removed and gate.sent==int(change=='valid')


def test_untrusted_instruction_cannot_arm_sports_plan(tmp_path,monkeypatch):
    f=Fake();e,_=make_engine(tmp_path,monkeypatch,f)
    p=e.prepare('sports','create_reservation',ARGS)
    e.consent_verifier=lambda _:None
    with pytest.raises(CampusError,match='TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED'):
        e.execute(p['plan_id'],AUTH)
    assert e.result(p['plan_id'])['status']=='PREPARED'


def test_unresolved_cancel_is_reconciled_after_switch_off(tmp_path,monkeypatch):
    f=Fake();f.rows=[{**ROW,'paidAmount':200}]
    e,_=make_engine(tmp_path,monkeypatch,f,'cancel_reservation')
    p=e.prepare('sports','cancel_reservation',{'record_id':'rec'})
    assert e.execute(p['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL'
    monkeypatch.setenv('THU_SPORTS_WRITE','0')
    f.rows[0].update(resvExtendStatus=2,orderRefundStatus=4)
    r=e.result(p['plan_id'])
    assert r['status']=='SUCCEEDED' and r['platform']['refund_status']=='REFUNDING'
