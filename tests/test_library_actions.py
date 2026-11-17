"""Synthetic business actions; no library submission, cancellation, or check-in."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
import json
import time
import pytest

from thu_assistant import spaces as s, library_actions as a, library_wire as w, library_contracts as c
from thu_assistant.actions import ActionEngine, RequestGate, NotDispatched
from thu_assistant.action_store import ActionStore
from synthetic_store import make_action_store
from thu_assistant.bridge import CampusError
from thu_assistant.browser import read_request
from thu_assistant.queries import describe
from thu_assistant.todos import normalize

DAY=(s.day()+timedelta(days=1)).isoformat()
SEAT={'area_id':'45','seat_id':'301','date':DAY,'start_time':'09:00','end_time':'11:00'}
ROOM={'room_id':'800','kind_id':'700','date':DAY,'start_time':'09:00','end_time':'11:00',
      'headcount':3,'members':['self','peer-1','peer-2'],'purpose':'合成讨论主题'}
START=DAY+'T09:00:00+08:00'
END=DAY+'T11:00:00+08:00'
AUTH='对本次合成测试的精确业务授权'
RULES={'timeInterval':5,'earliestResvTime':5760,'latestResvTime':0,'cancelTime':20,'minResvTime':30,
       'maxResvTime':240,'limit':0}
SEAT_AV={'items':[{'seat_id':'301','name':'N001','area_id':'45','free':True,'state':'FREE',
                   'state_mapping':'MAPPED','source_status_digest':'synthetic-digest','platform_status':1}],
         'free_count':1,'slot_free_count':1,'unknown_seat_count':0,'area_capacity':300,
         'capacity_scope':'AREA_TOTAL_SEATS_NOT_SLOT','date':DAY,'start_time':'09:00','end_time':'11:00',
         'complete':True,'resource':{'area_id':'45','path':['合成馆','A区'],'capacity':300},
         'url':s.SEAT+'/web/area','warnings':[],
         'segment_id':'123','official_page_url':s.SEAT+'/web/area?area=45','rules':['一次预约一个座位']}
MAP={'seat':[{'id':'301','control':'301','area':'45','name':'N001','status':1}],
     'sameName':1,'date':DAY,'start':'09:00','end':'11:00','segment':'123','area':'45','authenticated':True,'restricted':False}
SEAT_ROW={'reservation_id':'11','reservation_number':'R11','location':'合成馆-A区:N001','start_time':START,
          'end_time':END,'status_label':'已预约','cancel_control_visible':True}
ROOM_AV={'room_id':'800','name':'合成研讨间','min_people':3,'max_people':6,'only_view':False,'rules':RULES,
         'platform_open_state':0,'platform_device_status':0,'opening_intervals':[{'start_time':'08:00','end_time':'22:00'}],
         'reservation_notice':'合成须知','interval_state':'FREE_TIME_ONLY','party_state':'IN_RANGE'}
ROOM_ROW={'resvId':77,'uuid':'record-uuid','appAccNo':10,'resvStatus':2,'resvBeginTime':START,'resvEndTime':END,
          'resvDevInfoList':[{'devId':800,'devName':'合成研讨间','kindId':700}],'testName':ROOM['purpose'],
          'resvMemberInfoList':[{'accNo':10},{'accNo':11},{'accNo':12}]}


@pytest.fixture
def world(monkeypatch,tmp_path):
    obj=SimpleNamespace(seat=deepcopy(SEAT_AV),map=deepcopy(MAP),room=deepcopy(ROOM_AV),
                        seat_rows=[],room_rows=[],restrictions=0,verification='0',form_limit=0,
                        disabled=False,unresolved=set(),incomplete=set(),seat_detail_error=None,sent=0,lost=False,
                        delayed=False,path=None,body=None,after=None,store=make_action_store(tmp_path))
    class Reader:
        sid='session'
        def __init__(self,browsers,system):self.system=system;self.page=SimpleNamespace(evaluate=lambda *_:['当前取消规则'])
        def goto(self,path):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def call(self,path,**params):
            assert path=='/ic-web/roomMenu'
            return {'code':0,'data':[{'kindId':700,'kindClass':1,'kindName':'研讨类'}]}
    monkeypatch.setattr(s,'Reader',Reader)
    monkeypatch.setattr(s,'read_seat',lambda *args:deepcopy(obj.seat))
    def read_rooms(reader,op,params):
        room=deepcopy(obj.room)
        if 'people' in params:
            room['party_state']='PARTY_TOO_SMALL' if params['people']<room['min_people'] else 'PARTY_TOO_LARGE' if params['people']>room['max_people'] else 'IN_RANGE'
        return {'items':[room],'account_restrictions':{'active_records':obj.restrictions},'resource':{'name':'研讨类','class_kind':1}}
    monkeypatch.setattr(s,'read_rooms',read_rooms)
    monkeypatch.setattr(s,'seat_catalog',lambda reader,**kwargs:([{'area_id':'45','leaf':True,'path':['合成馆','A区']}],['当前取消规则']))
    monkeypatch.setattr(w,'seat_map',lambda *args:deepcopy(obj.map))
    monkeypatch.setattr(w,'seat_history',lambda *args:deepcopy(obj.seat_rows))
    monkeypatch.setattr(w,'room_history',lambda *args:deepcopy(obj.room_rows))
    def detail(reader,row):
        if obj.seat_detail_error:raise CampusError(obj.seat_detail_error)
        return {'number':row['reservation_number']}
    monkeypatch.setattr(a,'seat_detail',detail)
    monkeypatch.setattr(w,'room_identity',lambda reader:{'accNo':'10','login':'own','disabled':obj.disabled,
          'verification':obj.verification,'form':{'notice':False,'memo':0,'theme':1,'freeTitle':1,'cancellationReduction':0}})
    def member(reader,key):
        ids={'own':'10','peer-1':'11','peer-2':'12','duplicate':'10'}
        return {'complete':key not in obj.incomplete,'exact':[] if key in obj.unresolved or key not in ids
                else [{'accNo':ids[key],'disabled':obj.disabled}]}
    monkeypatch.setattr(w,'member_matches',member)
    obj.browsers=SimpleNamespace(sessions={'session':{'account_id':'account'}})
    def send(reader,system,path,body,gate):
        spec=w.specification(system,path,body)
        assert gate.consume(spec.url,'POST',spec.body)
        assert not gate.consume(spec.url,'POST',spec.body)
        obj.sent+=1;obj.path=path;obj.body=body
        if obj.after:obj.after()
        if obj.lost:raise TimeoutError()
        return True
    monkeypatch.setattr(w,'dispatch_exact',send)
    def grant(r):
        return {'account':r['account'],'digest':r['digest'],'plan_id':r['operation_id'],'adapter_version':c.VERSION,
                'reference':'synthetic-grant','source':'human','expires_at':r['expires_at']}
    obj.make=lambda:ActionEngine(store=obj.store,account_provider=lambda:'account',consent_verifier=grant,
                                 adapters={('library',op):a.LibraryAction(obj.browsers,op) for op in c.INPUTS})
    obj.engine=obj.make()
    return obj


def test_separate_contracts_and_flags(world,monkeypatch):
    ops={x['operation']:x for x in describe('library')['actions']}
    assert set(ops)==set(c.INPUTS)|set(c.RENEWAL_INPUTS)
    assert 'members' not in ops[c.CREATE_SEAT]['parameters_schema']['properties']
    assert ops[c.CREATE_ROOM]['parameters_schema']['properties']['members']['type']=='array'
    assert ops[c.RENEW_LOAN]['enabled'] is False
    assert ops[c.RENEW_LOAN]['reason']=='ADAPTER_REQUIRED'
    assert ops[c.RENEW_LOAN]['reconciliation']=='OWN_LOAN_SAME_RECORD_AND_NEW_DUE_AT'
    monkeypatch.setenv('THU_SPORTS_WRITE','0')
    assert all(x['enabled'] for x in describe('library')['actions'] if x['operation']!=c.RENEW_LOAN)
    monkeypatch.setenv('THU_LIBRARY_WRITE','0')
    assert not any(x['enabled'] for x in describe('library')['actions'])
    assert any(x['operation']=='reservations' for x in describe('library')['operations'])


def test_members_only_in_encrypted_state(world):
    p=world.engine.prepare('library',c.CREATE_ROOM,ROOM)
    out=json.dumps(p,ensure_ascii=False)
    assert p['status']=='PREPARED' and p['preview']['member_count']==3
    assert 'peer-1' not in out and ROOM['purpose'] not in out
    data=world.store.read(p['plan_id'])
    assert data['business']['resolved_members']==['10','11','12']
    assert b'peer-1' not in world.store.path(p['plan_id']).read_bytes()


@pytest.mark.parametrize('params,code',[
    ({'headcount':2,'members':['self','peer-1']},'PARTY_TOO_SMALL'),
    ({'headcount':4},'ROOM_MEMBERS_REQUIRED'),({'members':['self','peer-1','peer-1']},'ROOM_DUPLICATE_MEMBER'),
    ({'members':['self','duplicate','peer-1']},'ROOM_DUPLICATE_MEMBER'),
    ({'members':['peer-1','self','peer-2']},'ROOM_APPLICANT_MUST_BE_FIRST'),
    ({'start_time':'09:02'},'ROOM_TIME_GRID_MISMATCH'),
    ({'date':(s.day()+timedelta(days=8)).isoformat()},'ROOM_OUTSIDE_BOOKING_WINDOW'),
    ({'date':(s.day()-timedelta(days=1)).isoformat()},'LIBRARY_START_IN_PAST'),
    ({'members':'placeholder'},'INVALID_ARGUMENT_TYPE')])
def test_room_input_and_opening_rules(world,params,code):
    with pytest.raises(CampusError,match=code):world.engine.prepare('library',c.CREATE_ROOM,{**ROOM,**params})
    assert world.sent==0


@pytest.mark.parametrize('condition,code',[('occupied','ROOM_BUSY'),('restriction','ROOM_ACCOUNT_RESTRICTED'),
                         ('disabled','ROOM_ACCOUNT_RESTRICTED'),('view','ROOM_VIEW_ONLY')])
def test_room_restrictions(world,condition,code):
    if condition=='occupied':world.room['interval_state']='BUSY'
    if condition=='restriction':world.restrictions=1
    if condition=='disabled':world.disabled=True
    if condition=='view':world.room['only_view']=True
    with pytest.raises(CampusError,match=code):world.engine.prepare('library',c.CREATE_ROOM,ROOM)


@pytest.mark.parametrize('reason', ['masked','ambiguous','truncated','captcha','form'])
def test_official_handoff_keeps_members_incomplete_and_sends_nothing(world,reason):
    args=deepcopy(ROOM)
    if reason=='masked':args['members'][2]='王*'
    if reason=='ambiguous':world.unresolved={'peer-2'}
    if reason=='truncated':world.incomplete={'peer-2'}
    if reason=='captcha':world.verification='2'
    if reason=='form':world.room['rules']['limit']=8192
    p=world.engine.prepare('library',c.CREATE_ROOM,args)
    assert p['preview']['handoff']
    result=world.engine.execute(p['plan_id'],AUTH)
    assert result['status']=='AWAITING_OFFICIAL' and world.sent==0
    assert world.engine.execute(p['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL' and world.sent==0


def test_preflight_changed_map_or_members_binds_new_preview(world):
    p=world.engine.prepare('library',c.CREATE_ROOM,ROOM)
    world.unresolved={'peer-2'}
    result=world.engine.execute(p['plan_id'],AUTH)
    assert result['status']=='FAILED' and world.sent==0


@pytest.mark.parametrize('change,expect',[('map','AWAITING_OFFICIAL'),('api-free','SEAT_UNAVAILABLE'),
    ('map-free','SEAT_UNAVAILABLE'),('restricted','SEAT_ACCOUNT_RESTRICTED'),('other','SEAT_NOT_IN_SELECTED_AREA'),
    ('overlap','SEAT_EXISTING_OVERLAP'),('window','DATE_NOT_OFFERED')])
def test_seat_requires_unique_map_and_platform_slot(world,change,expect):
    if change=='map':world.map['sameName']=2
    if change=='api-free':world.seat['items'][0]['free']=False
    if change=='map-free':world.map['seat'][0]['status']=2
    if change=='restricted':world.map['restricted']=True
    if change=='other':world.seat['items']=[]
    if change=='overlap':world.seat_rows=[SEAT_ROW]
    if change=='window':world.seat.update(status='NOT_OPEN',reason='DATE_NOT_OFFERED')
    if change=='map':
        p=world.engine.prepare('library',c.CREATE_SEAT,SEAT)
        assert world.engine.execute(p['plan_id'],AUTH)['status']==expect
    else:
        with pytest.raises(CampusError,match=expect):world.engine.prepare('library',c.CREATE_SEAT,SEAT)
    assert world.sent==0


def test_seat_response_lost_unique_detail_reconciles_after_restart(world):
    p=world.engine.prepare('library',c.CREATE_SEAT,SEAT)
    world.lost=True
    assert world.engine.execute(p['plan_id'],AUTH)['status']=='UNKNOWN'
    world.seat_rows=[deepcopy(SEAT_ROW)]
    restored=world.make()
    assert restored.result(p['plan_id'])['status']=='SUCCEEDED'
    assert restored.execute(p['plan_id'],AUTH)['status']=='SUCCEEDED' and world.sent==1


@pytest.mark.parametrize('case,expected',[('normal','SUCCEEDED'),('review','PENDING_APPROVAL'),('duplicates','UNKNOWN'),
    ('missing_members','UNKNOWN'),('other_members','UNKNOWN'),('denied','FAILED')])
def test_room_records_match_exact_members_and_decision(world,case,expected):
    r=deepcopy(ROOM_ROW)
    if case=='review':r['resvStatus']=256
    if case=='denied':r['resvStatus']=512
    if case=='missing_members':del r['resvMemberInfoList']
    if case=='other_members':r['resvMemberInfoList'][2]['accNo']=13
    world.after=lambda:world.room_rows.extend([r,{**r,'resvId':78}] if case=='duplicates' else [r])
    p=world.engine.prepare('library',c.CREATE_ROOM,ROOM)
    result=world.engine.execute(p['plan_id'],AUTH)
    assert result['status']==expected and world.sent==1
    assert world.body['resvMember']==[10,11,12] and world.body['resvDev']==[800]
    assert world.body['memberKind']==2 and world.body['testName']==ROOM['purpose']
    assert world.engine.execute(p['plan_id'],AUTH)['status']==expected and world.sent==1


@pytest.mark.parametrize('system',['seat','room'])
@pytest.mark.parametrize('reply,changed,expected',[(True,True,'SUCCEEDED'),(False,True,'UNKNOWN'),(True,False,'UNKNOWN')])
def test_cancel_readback_and_receipt_are_separate(world,system,reply,changed,expected):
    world.seat_rows=[deepcopy(SEAT_ROW)];world.room_rows=[deepcopy(ROOM_ROW)]
    rows=world.seat_rows if system=='seat' else world.room_rows
    args={'system':system,'date':DAY,'record_id':'11' if system=='seat' else '77'}
    p=world.engine.prepare('library',c.CANCEL,args)
    if changed:world.after=rows.clear
    world.lost=not reply
    result=world.engine.execute(p['plan_id'],AUTH)
    assert result['status']==expected and world.sent==1
    assert world.make().execute(p['plan_id'],AUTH)['status']==expected and world.sent==1


def test_seat_cancel_label_and_todo_agree_without_resend(world):
    world.seat_rows=[deepcopy(SEAT_ROW)];world.lost=True
    args={'system':'seat','date':DAY,'record_id':'11'}
    p=world.engine.prepare('library',c.CANCEL,args)
    world.after=lambda:world.seat_rows[0].update(status_label='用户取消')
    assert world.engine.execute(p['plan_id'],AUTH)['status']=='SUCCEEDED'
    assert normalize('library.seat',world.seat_rows[0],datetime.now(s.TZ).isoformat())['status']=='CANCELLED'


def test_disabled_switch_allows_existing_plan_readback(world,monkeypatch):
    p=world.engine.prepare('library',c.CREATE_ROOM,ROOM)
    assert world.engine.execute(p['plan_id'],AUTH)['status']=='UNKNOWN'
    monkeypatch.setenv('THU_LIBRARY_WRITE','0')
    world.room_rows=[ROOM_ROW]
    assert world.make().result(p['plan_id'])['status']=='SUCCEEDED'
    with pytest.raises(CampusError,match='LIBRARY_WRITES_DISABLED'):world.engine.prepare('library',c.CREATE_SEAT,SEAT)


@pytest.mark.parametrize('url',[s.ROOM+'/ic-web/reserve',s.ROOM+'/ic-web/reserve/delete',
    s.SEAT+'/api.php/spaces/301/book',s.SEAT+'/api.php/profile/books/11',
    'https://webvpn.tsinghua.edu.cn/https/abc123/api.php/profile/books/11'])
def test_general_browser_denies_library_posts(url):
    assert not read_request(url,'POST','{}')


class Page:
    main_frame=object()
    def __init__(self,url,body,variant,system):
        self.url,self.body,self.variant,self.system=url,body,variant,system
        self.continued=0
    def evaluate(self,script,args):
        import hashlib
        if script==w._PREPARE:return hashlib.sha256(self.body.encode()).hexdigest()
        raw=self.body+'!' if self.variant=='body' else self.body
        req=SimpleNamespace(url=self.url,method='GET' if self.variant=='method' else 'POST',
                            frame=object() if self.variant=='frame' else self.main_frame,post_data=raw)
        self.guard(SimpleNamespace(request=req,abort=lambda:None,continue_=lambda:setattr(self,'continued',self.continued+1)))
        return True
    def route(self,pattern,guard):assert pattern.fullmatch(self.url);self.guard=guard
    def unroute(self,*args):self.cleaned=True


@pytest.mark.parametrize('system',['seat','room'])
@pytest.mark.parametrize('variant',['good','body','method','frame'])
def test_exact_wire_binds_actual_body_and_frame(system,variant):
    path='/api.php/profile/books/11' if system=='seat' else '/ic-web/reserve/delete'
    fields={'_method':'delete','id':'11','operateChannel':2} if system=='seat' else {'uuid':'record-uuid'}
    spec=w.specification(system,path,fields);gate=RequestGate([spec])
    body=spec.body+('&userid=synthetic&access_token=synthetic' if system=='seat' else '')
    page=Page(spec.url,body,variant,system);reader=SimpleNamespace(page=page,base=s.SEAT)
    if variant=='good':
        assert w.dispatch_exact(reader,system,path,fields,gate) is True
    else:
        with pytest.raises(NotDispatched):w.dispatch_exact(reader,system,path,fields,gate)
    assert gate.sent==page.continued==int(variant=='good') and page.cleaned


def test_catalog_reads_each_official_root_regardless_of_homepage_order():
    calls=[]
    class R:
        page=SimpleNamespace(locator=lambda _:SimpleNamespace(evaluate_all=lambda _:['6','35']),
                             evaluate=lambda *args:['rules'])
        def goto(self,path):pass
        def call(self,path):
            key=path.split('/')[-1];calls.append(key)
            tree=[{'id':int(key),'parentId':0,'name':'馆'+key,'isValid':1},
                  {'id':100+int(key),'parentId':int(key),'name':'阅览区','type':1,'isValid':1}]
            return {'status':1,'data':{'list':{'seatinfo':tree}}}
    rows,_=s.seat_catalog(R())
    assert calls==['6','35'] and {x['area_id'] for x in rows}=={'6','106','35','135'}


def test_member_lookup_not_exposed_by_browser_observer():
    from thu_assistant.browser import safe_url
    assert safe_url(s.ROOM+'/ic-web/account/getMembers?key=synthetic-person&page=1')==s.ROOM+'/ic-web/account/getMembers'


def test_wrong_detail_prevents_seat_success(world):
    p=world.engine.prepare('library',c.CREATE_SEAT,SEAT)
    world.seat_detail_error='LIBRARY_SEAT_DETAIL_MISMATCH'
    world.after=lambda:world.seat_rows.extend([deepcopy(SEAT_ROW)])
    assert world.engine.execute(p['plan_id'],AUTH)['status']=='UNKNOWN'


def test_missing_cancel_button_rejects_before_dispatch(world):
    world.seat_rows=[{**SEAT_ROW,'cancel_control_visible':False}]
    with pytest.raises(CampusError,match='SEAT_CANCELLATION_UNAVAILABLE'):
        world.engine.prepare('library',c.CANCEL,{'system':'seat','date':DAY,'record_id':'11'})
    assert world.sent==0


def test_approved_business_returns_a_fresh_todo_selector(world):
    p=world.engine.prepare('library',c.CREATE_ROOM,ROOM)
    world.after=lambda:world.room_rows.extend([{**ROOM_ROW,'resvStatus':256}])
    result=world.engine.execute(p['plan_id'],AUTH)
    assert result['status']=='PENDING_APPROVAL'
    assert result['todo_query']=={'services':['library.room'],'start_date':DAY,'end_date':DAY,'include_completed':True}
    before=normalize('library.room',s.own_room_row(world.room_rows[0]),datetime.now(s.TZ).isoformat())
    assert before['status']=='PENDING_APPROVAL'
    world.room_rows[0]['resvStatus']=4
    assert world.make().result(p['plan_id'])['status']=='SUCCEEDED'
    assert normalize('library.room',s.own_room_row(world.room_rows[0]),datetime.now(s.TZ).isoformat())['status']=='SCHEDULED'

def test_inactive_seat_area_is_distinct_from_missing_or_sold_out(monkeypatch):
    node={'area_id':'45','name':'已关闭区域','currently_enabled':False,'leaf':True,'path':['馆','区域']}
    monkeypatch.setattr(s,'seat_catalog',lambda reader,**kwargs:([node],[]))
    result=s.read_seat(object(),'seat_availability',{'area_id':'45','date':DAY})
    assert result['status']=='NOT_OPEN' and result['reason']=='AREA_NOT_CURRENTLY_ENABLED'
    assert 'free_count' not in result
    assert s.read_seat(object(),'seat_availability',{'area_id':'99','date':DAY})['status']=='NOT_FOUND'


def test_nested_directory_entry_preserves_full_location_for_record_readback():
    base={'isValid':1,'type':0}
    rows=[{**base,'id':1,'parentId':0,'name':'馆'},{**base,'id':2,'parentId':1,'name':'层'},
          {**base,'id':3,'parentId':2,'name':'区','isValid':0,'type':1}]
    assert len(s.area_rows({'seatinfo':rows},{'1','2'}))==2
    all_rows=s.area_rows({'seatinfo':rows},{'1','2'},True)
    assert all_rows[-1]['path']==['馆','层','区'] and not all_rows[-1]['currently_enabled']


def test_seat_map_control_change_rejects_the_old_plan_without_dispatching(world):
    p=world.engine.prepare('library',c.CREATE_SEAT,SEAT)
    assert p['preview']['precheck']['availability_state']=='FREE'
    # The platform renumbers the map control while keeping the numeric id.
    world.map['seat'][0]['control']='999'
    assert world.engine.execute(p['plan_id'],AUTH)['status']=='FAILED' and world.sent==0


def test_seat_unknown_state_needs_the_official_map_and_never_dispatches(world):
    world.seat['items'][0].update(free=None,state='UNKNOWN',state_mapping='UNMAPPED')
    world.seat['unknown_seat_count']=1
    p=world.engine.prepare('library',c.CREATE_SEAT,SEAT)
    assert 'OFFICIAL_SEAT_SELECTION' in p['preview']['handoff']
    assert p['preview']['precheck']['availability_state']=='UNKNOWN'
    assert world.engine.execute(p['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL' and world.sent==0


def test_room_rule_gap_hands_off_a_duration_recheck(world):
    world.room['interval_state']='RULE_UNVERIFIED'
    p=world.engine.prepare('library',c.CREATE_ROOM,ROOM)
    assert 'CONFIRM_ROOM_DURATION_RULE' in p['preview']['handoff']
    assert p['preview']['precheck']['availability_state']=='RULE_UNVERIFIED'
    assert world.engine.execute(p['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL' and world.sent==0


def test_room_cancel_reads_current_rules_without_a_selected_booking_window(world):
    world.room_rows=[deepcopy(ROOM_ROW)]
    world.room['interval_state']='NOT_SELECTED'
    plan=world.engine.prepare('library',c.CANCEL,{'system':'room','date':DAY,'record_id':'77'})
    assert plan['status']=='PREPARED' and world.sent==0
    assert '20' in plan['preview']['cancel_rule']


@pytest.mark.parametrize('case,code',[('seat_missing','SEAT_OWN_RECORD_NOT_FOUND'),
    ('room_missing','ROOM_OWN_RECORD_NOT_FOUND'),('applicant','ROOM_APPLICANT_MISMATCH')])
def test_cancel_rejects_records_outside_the_current_account(world,case,code):
    if case=='seat_missing':
        world.room_rows=[deepcopy(ROOM_ROW)]
        args={'system':'seat','date':DAY,'record_id':'11'}
    elif case=='room_missing':
        world.seat_rows=[deepcopy(SEAT_ROW)]
        args={'system':'room','date':DAY,'record_id':'77'}
    else:
        world.room_rows=[{**deepcopy(ROOM_ROW),'appAccNo':99}]
        args={'system':'room','date':DAY,'record_id':'77'}
    with pytest.raises(CampusError,match=code):
        world.engine.prepare('library',c.CANCEL,args)
    assert world.sent==0


def test_cancel_system_mismatch_does_not_fall_through_to_the_other_system(world):
    world.seat_rows=[deepcopy(SEAT_ROW)]
    world.room_rows=[]
    with pytest.raises(CampusError,match='ROOM_OWN_RECORD_NOT_FOUND'):
        world.engine.prepare('library',c.CANCEL,{'system':'room','date':DAY,'record_id':'11'})
    assert world.sent==0


def test_empty_cancel_list_without_receipt_stays_unknown(world):
    world.seat_rows=[deepcopy(SEAT_ROW)]
    plan=world.engine.prepare('library',c.CANCEL,{'system':'seat','date':DAY,'record_id':'11'})
    world.lost=True
    world.after=lambda:world.seat_rows.clear()
    assert world.engine.execute(plan['plan_id'],AUTH)['status']=='UNKNOWN' and world.sent==1
    assert world.make().result(plan['plan_id'])['status']=='UNKNOWN' and world.sent==1
