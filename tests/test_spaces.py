from copy import deepcopy
from types import SimpleNamespace
import pytest
from thu_assistant import spaces as s
from thu_assistant.bridge import CampusError
from thu_assistant.browser import read_request
from thu_assistant.queries import QueryEngine,describe,validate,SPACE_OPS

def seat(rows):return {'status':1,'data':{'list':rows}}
def room(rows,count=None):return {'code':0,'data':rows,'count':len(rows) if count is None else count}
def fixture_room():
    return {'devId':1,'devName':'测试间','kindId':12,'minUser':3,'maxUser':10,
            'openState':1,'devStatus':1,'openTimes':[{'openStartTime':'08:00','openEndTime':'22:00','openLimit':1}],
            'resvInfo':[{'startTime':'2026-11-06 10:00:00','endTime':'2026-11-06 11:00:00',
                         'resvStatus':2,'trueName':'DO_NOT_LEAK','logonName':'DO_NOT_LEAK','title':'DO_NOT_LEAK'}],
            'resvRule':{'minResvTime':30,'maxResvTime':240,'cancelTime':20,'uuid':'DO_NOT_LEAK','memo':'<b>官方规则</b>'}}

@pytest.mark.parametrize('value',['2026-02-30','2026-13-01','x'])
def test_invalid_calendar_date(value):
    with pytest.raises(CampusError,match='INVALID_DATE'):s.day(value)

@pytest.mark.parametrize('params',[{'start_time':'08:00'},{'start_time':'10:00','end_time':'09:00'},{'start_time':'24:00','end_time':'24:01'}])
def test_invalid_time_pair(params):
    with pytest.raises(CampusError):s.times(params)

@pytest.mark.parametrize('payload',[{},'<html>login</html>',{'code':0,'data':None},{'code':500,'data':[]}])
def test_shell_never_empty(payload):
    with pytest.raises(CampusError):s.check(payload,'room')

@pytest.mark.parametrize('payload',[{'status':0,'msg':'没有登录或登录已超时'},{'status':401}])
def test_expired_seat(payload):
    with pytest.raises(CampusError,match='AUTH_REQUIRED'):s.check(payload,'seat')

def test_permission_and_business_failure_differ():
    with pytest.raises(CampusError,match='FORBIDDEN'):s.check({'code':403},'room')
    with pytest.raises(CampusError,match='SPACE_QUERY_REJECTED'):s.check({'code':1,'message':'日期不能为空'},'room')

def test_directory_hierarchy_and_hidden():
    rows=[{'id':1,'name':'馆','parentId':0,'isValid':1,'type':0},
          {'id':2,'name':'一层','parentId':1,'isValid':1,'type':0},
          {'id':3,'name':'A区','parentId':2,'isValid':1,'type':1,'TotalCount':30},
          {'id':4,'name':'不开放','parentId':1,'isValid':0,'type':1},
          {'id':5,'name':'隐藏','parentId':1,'isValid':1,'webhidden':1},
          {'id':6,'name':'旧馆','parentId':0,'isValid':1}]
    out=s.area_rows({'seatinfo':rows},{'1'})
    assert len(out)==3 and out[-1]['path']==['馆','一层','A区']
    assert out[-1]['availability'] is None and out[-1]['capacity']==30

def test_duplicate_and_cycle_directory_fail():
    node={'id':1,'name':'A','parentId':2}
    with pytest.raises(CampusError,match='DUPLICATE'):s.area_rows({'seatinfo':[node,node]},{'1'})
    with pytest.raises(CampusError,match='CYCLE'):s.area_rows({'seatinfo':[node,{'id':2,'name':'B','parentId':1}]},set())

def test_exact_name_ambiguity_and_id():
    rows=[{'area_id':'1','name':'A区','path':['一馆','A区']},{'area_id':'2','name':'A区','path':['二馆','A区']}]
    assert s.select(rows,{'name':'A区'},'area_id','name')[1]['status']=='NEEDS_USER_ACTION'
    assert s.select(rows,{'name':'一馆/A区'},'area_id','name')[0]['area_id']=='1'
    assert s.select(rows,{'area_id':'2'},'area_id','name')[0]['area_id']=='2'
    assert s.select(rows,{'name':'A'},'area_id','name')[1]['status']=='NOT_FOUND'

def test_seat_date_and_status_validation():
    slot={'id':12,'day':'2026-11-06','startTime':'08:00','endTime':'22:00','status':1}
    assert s.seat_slots(seat([slot]),'2026-11-06')[0]['segment_id']=='12'
    with pytest.raises(CampusError,match='DATE_MISMATCH'):s.seat_slots(seat([slot]),'2026-11-07')
    states=s.seat_states(seat([{'id':n,'name':str(n),'area':45,'status':n} for n in range(1,10)]),'45')
    assert sum(x['free'] for x in states)==1
    assert states[-1]['state']=='TIME_EXPIRED'
    with pytest.raises(CampusError,match='RESOURCE_MISMATCH'):s.seat_states(seat([{'id':1,'area':2}]),'45')

@pytest.mark.parametrize('wanted,expected',[
    (('09:00','10:00'),'FREE_TIME_ONLY'),(('10:00','10:30'),'BUSY'),
    (('11:00','12:00'),'FREE_TIME_ONLY'),(('07:00','08:00'),'NOT_OPEN'),
    (('11:00','11:05'),'DURATION_OUT_OF_RANGE'),(('11:00','16:00'),'DURATION_OUT_OF_RANGE')])
def test_room_overlap_and_boundaries(wanted,expected):
    row=s.room_row(fixture_room(),'2026-11-06',3,wanted)
    assert row['interval_state']==expected and row['eligibility']=='UNVERIFIED'
    assert 'DO_NOT_LEAK' not in str(row) and row['rules']['memo']=='官方规则'

@pytest.mark.parametrize('people,state',[(None,'UNSPECIFIED'),(2,'PARTY_TOO_SMALL'),(3,'IN_RANGE'),(10,'IN_RANGE'),(11,'PARTY_TOO_LARGE')])
def test_room_party(people,state):
    assert s.room_row(fixture_room(),'2026-11-06',people,None)['party_state']==state

@pytest.mark.parametrize('rows,total,page,size,next_page',[([],0,1,10,None),([{}]*10,11,1,10,2),([{}],11,2,10,None),([],11,3,10,None)])
def test_room_pagination(rows,total,page,size,next_page):
    _,meta=s.page_window(room(rows,total),page,size)
    assert meta['next_page']==next_page and meta['upstream_total']==total

def test_partial_page_not_complete():
    with pytest.raises(CampusError,match='PAGINATION_CHANGED'):s.page_window(room([{}],20),1,10)
    with pytest.raises(CampusError):s.page_window({'code':0,'data':[]},1,10)

def own_table(total=1,page=1):
    rows=[]
    for n in range((page-1)*20,min(page*20,total)):
        rows.append({'key':'list_'+str(n+1),'cells':[str(10000+n),'馆/区/座位','2026-11-06 09:00:00','2026-11-06 10:00:00','预约成功','','取消 查看']})
    return {'headers':['预约号','预约空间','开始时间','结束时间','当前状态','预约目的','操作'],
            'rows':rows,'pagination':f'共 {total} 条记录','current':[str(page)]}

def test_own_seat_pages_and_stable_record_ids():
    a,first=s.parse_seat_reservations(own_table(21),1)
    b,last=s.parse_seat_reservations(own_table(21,2),2)
    assert len(a)==20 and first['next_page']==2 and last['next_page'] is None
    assert {x['reservation_id'] for x in a}.isdisjoint({x['reservation_id'] for x in b})
    assert b[0]['cancel_control_visible'] and b[0]['start_time'].endswith('+08:00')

def test_empty_seat_requires_official_total_marker():
    assert s.parse_seat_reservations(own_table(0),1)[0]==[]
    shell=own_table(0);shell['pagination']='加载中'
    with pytest.raises(CampusError,match='TOTAL_MISSING'):s.parse_seat_reservations(shell,1)
    shell=own_table(40,1)
    with pytest.raises(CampusError,match='PAGE_MISMATCH'):s.parse_seat_reservations(shell,2)

def test_own_room_allowlist():
    row={'resvId':1,'resvStatus':2,'resvDevInfoList':[{'devId':2,'devName':'测试间','trueName':'DO_NOT_LEAK'}],
         'resvBeginTime':1793926800000,'resvEndTime':1793930400000,'resvMemberInfoList':[{'trueName':'DO_NOT_LEAK'}]}
    out=s.own_room_row(row)
    assert out['reservation_id']=='1' and 'DO_NOT_LEAK' not in str(out)

def test_read_contract_and_browser_write_barrier():
    read=s.ROOM+'/ic-web/reserve?sysKind=1&resvDates=20261106&kindIds=12&page=1&pageSize=10'
    own=s.ROOM+'/ic-web/reserve/resvInfo?beginDate=2026-11-01&endDate=2026-11-30&needStatus=6&page=1&pageNum=10'
    assert read_request(read) and read_request(own)
    for url in [read,own,s.SEAT+'/api.php/spaces/1/book',s.SEAT+'/user/index/book',s.ROOM+'/ic-web/reserve/endReserve']:
        assert not read_request(url,'POST')
    assert not read_request(read+'&action=cancel')
    assert not read_request(s.ROOM+'/ic-web/reserve?resvId=1')
    assert read_request(s.SEAT+'/user/index/book/p/2')
    assert not read_request(s.SEAT+'/user/index/book/p/2/cancel')
    assert not s.allowed_read('seat','/api.php/profile/books/1',{})
    assert not s.allowed_read('room','/ic-web/reserve',{'resvId':1})
    assert not s.allowed_read('seat','https://evil.example/api.php/spaces',{})

def test_query_validation_before_browser():
    for params in [{'area_id':'1','date':'2026-02-30'},{'area_id':'1','start_time':'09:00'},
                   {'area_id':'1','name':'A'}]:
        with pytest.raises(CampusError):s.query(None,'seat_availability',params)

def test_private_cursor_freshness_and_expiry(monkeypatch):
    account={'account_id':'one'};calls=[]
    engine=QueryEngine(SimpleNamespace(bridge=lambda *a:account,browsers=None))
    def fake(*args):
        calls.append(1);return s.result([{'id':'1'},{'id':'2'}],s.SEAT)
    monkeypatch.setattr(s,'query',fake)
    p=engine.query('library','seat_availability',{'area_id':'1'},page_size=1)
    assert p['freshness']=='LIVE_AT_CHECKED_AT' and not p['reusable_for_booking']
    engine.query('library','seat_availability',{'area_id':'1'},cursor=p['next_cursor'],page_size=1)
    assert len(calls)==1
    account['account_id']='two'
    with pytest.raises(CampusError,match='CURSOR_QUERY_MISMATCH'):
        engine.query('library','seat_availability',{'area_id':'1'},cursor=p['next_cursor'],page_size=1)
    engine.query('library','seat_availability',{'area_id':'1'},page_size=1)
    assert len(calls)==2
    engine.snapshots.clock=lambda:1e30
    with pytest.raises(CampusError,match='CURSOR_EXPIRED'):
        engine.query('library','seat_availability',{'area_id':'1'},cursor=p['next_cursor'],page_size=1)

@pytest.mark.parametrize('status',['NOT_OPEN','NEEDS_USER_ACTION','NOT_FOUND'])
def test_nonempty_semantic_status_preserved(monkeypatch,status):
    monkeypatch.setattr(s,'query',lambda *args:s.result([],s.SEAT,status=status))
    e=QueryEngine(SimpleNamespace(bridge=lambda *a:{'account_id':'one'},browsers=None))
    assert e.query('library','seat_availability',{})['status']==status

def test_registry_and_separate_disable(monkeypatch):
    assert set(SPACE_OPS)<={x['operation'] for x in describe('library')['operations']}
    monkeypatch.setenv('THU_SPACE_READ','0')
    names={x['operation'] for x in describe('library')['operations']}
    assert 'catalog_search' in names and not set(SPACE_OPS)&names
    with pytest.raises(CampusError):validate({'system':'both'},SPACE_OPS['reservations'])
    with pytest.raises(CampusError):validate({'kind_id':'1','token':'x'},SPACE_OPS['room_availability'])

def test_failed_login_closes_owned_browser():
    class Browser:
        sessions={'one':{'page':SimpleNamespace(url='https://id.tsinghua.edu.cn/login')}}
        closed=False
        def open(self,**kw):return {'session_id':'one','status':'OK'}
        def close(self,sid):self.closed=True
    browser=Browser()
    with pytest.raises(CampusError,match='AUTH_REQUIRED'):
        with s.Reader(browser,'seat'):pass
    assert browser.closed

@pytest.mark.parametrize('mode',['closed','map_only','empty','expired','custom_time'])
def test_seat_query_distinguishes_not_open_map_empty_and_expired(monkeypatch,mode):
    calls=[]
    resource={'area_id':'45','name':'A区','leaf':True,'parent_id':'37'}
    monkeypatch.setattr(s,'seat_catalog',lambda reader,**kwargs:([{**resource,'currently_enabled':True}],[]))
    class Reader:
        page=SimpleNamespace(evaluate=lambda *a:['<b>当前规则</b>'])
        def goto(self,path):pass
        def call(self,path,**params):
            calls.append(path)
            if 'space_days' in path:return seat([{'day':'2026-11-06'}])
            if 'time_buckets' in path:return seat([{'id':1,'day':'2026-11-06','startTime':'08:00','endTime':'22:00','status':1}])
            if mode=='map_only':return seat({'canvas':True})
            if mode=='expired':return {'status':0,'msg':'没有登录或登录已超时'}
            return seat([])
    params={'area_id':'45','date':'2026-11-07' if mode=='closed' else '2026-11-06'}
    if mode=='custom_time':params.update(start_time='09:00',end_time='11:00')
    if mode=='expired':
        with pytest.raises(CampusError,match='AUTH_REQUIRED'):s.read_seat(Reader(),'seat_availability',params)
        return
    out=s.read_seat(Reader(),'seat_availability',params)
    if mode=='custom_time':
        assert out['status']=='NEEDS_USER_ACTION' and out['reason']=='PLATFORM_TIME_SLOT_REQUIRED'
        assert not any('spaces_old' in c for c in calls)
    elif mode=='closed':
        assert out['status']=='NOT_OPEN' and not any('spaces_old' in c for c in calls)
    elif mode=='map_only':
        assert out['status']=='BROWSER_REQUIRED' and not out['complete'] and not out['items']
    else:
        assert out['items']==[] and out['free_count']==0 and out['complete']

