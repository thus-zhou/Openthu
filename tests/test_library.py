from types import SimpleNamespace
import pytest

from thu_assistant import library as lib
from thu_assistant.bridge import CampusError
from thu_assistant.queries import QueryEngine, describe


def doc(rid='rec1', categories=None):
    return {'pnx':{'control':{'recordid':[rid]},'display':{'title':['同名书目']},
                   'search':{'addsrcrecordid':['123']},'links':{'linktorsrc':['$$Uhttps://books.example/item?id=1$$Eview']}},
            'delivery':{'deliveryCategory':categories or ['Alma-P']}}

def wrap(value):
    return {'code':200,'data':value}

def test_large_search_window_respects_source_count_and_editions():
    def call(op, **params):
        assert op=='search' and params['search']=='creator,contains,作者' and params['offset']==50
        return wrap({'info':{'first':'51','total':960},'docs':[doc('rec1'),doc('rec2')]})
    result=lib.catalog_search(call,{'query':'作者','field':'author','offset':50,'limit':2})
    assert result['next_offset']==52 and result['upstream_total']==960 and result['complete']
    assert [r['record_id'] for r in result['items']]==['rec1','rec2']

def test_zero_hits_requires_valid_total_and_payload():
    p={'query':'none'}
    assert lib.catalog_search(lambda *a,**kw:wrap({'info':{'total':0},'docs':[]}),p)['items']==[]
    with pytest.raises(CampusError):
        lib.catalog_search(lambda *a,**kw:wrap({'docs':[]}),p)
    with pytest.raises(CampusError):
        lib.catalog_search(lambda *a,**kw:{'code':-1,'data':[]},p)

def test_missing_or_repeated_search_page_is_partial():
    def call(*args,**params):
        return wrap({'info':{'total':100,'first':1},'docs':[doc(),doc()]})
    result=lib.catalog_search(call,{'query':'book','offset':10})
    assert not result['complete'] and result['next_offset'] is None
    assert 'DUPLICATE_RECORD_ID' in result['warnings']

def holding_call(op,**params):
    if op=='detail':
        return wrap(doc(categories=['Alma-E','Alma-P']))
    if op=='holdings':
        return wrap({'total_record_count':2,'holding':[
            {'suppress_from_publishing':'true','holding_id':'991'},
            {'suppress_from_publishing':'false','holding_id':'992','call_number':'TV1','library':{'desc':'分馆'}}]})
    assert params['holdingid']=='992' and params['userid']==''
    return wrap({'total_record_count':1,'item':[{'holding_data':{'holding_id':'992'},'bib_data':{'mms_id':'123'},
                'item_data':{'pid':'987','process_type':{'value':'LOAN','desc':'借出'},'policy':{'desc':'普通外借'}}}]})

def test_mixed_record_suppression_copy_status_and_electronic_access():
    item=lib.catalog_detail(holding_call,{'record_id':'rec1'})['items'][0]
    assert item['electronic_links']==['https://books.example/item?id=1']
    assert item['electronic_access_page']==item['detail_url']
    assert len(item['holdings'])==1 and item['holdings'][0]['call_number']=='TV1'
    copy=item['holdings'][0]['copies'][0]
    assert copy['status']=='借出' and copy['borrowable'] is False

def test_copy_permission_failure_keeps_bibliography_and_location():
    def denied(op,**params):
        if op=='copies':
            return {'code':403}
        return holding_call(op,**params)
    response=lib.catalog_detail(denied,{'record_id':'rec1'})
    assert not response['complete'] and 'FORBIDDEN' in response['warnings']
    assert response['items'][0]['holdings'][0]['library']=='分馆'

def test_wrong_detail_id_fails():
    with pytest.raises(CampusError):
        lib.catalog_detail(holding_call,{'record_id':'other'})

def loans(rows,more='N'):
    return wrap({'data':{'loans':{'loan':rows,'showmore':[more]}}})

def test_date_precision_and_renewal_hints():
    rows,_=lib.parse_loans(loans([{'loanid':'1','title':'书','duedate':'20010102','loandate':'20001201',
                                   'renew':'N','renewstatuses':{'renewstatus':['已达上限']}},
                                 {'loanid':'2','title':'资料'},{'loanid':'3','title':'书','duedate':'20991231','duehour':'1530','renew':'Y'}]))
    assert rows[0]['overdue'] and rows[0]['due_precision']=='date' and rows[0]['due_time'] is None
    assert rows[0]['renewable'] is False and rows[0]['renewal_hints']==['已达上限']
    assert rows[1]['due_date'] is None and rows[1]['renewable'] is None
    assert rows[2]['due_time']=='15:30' and rows[2]['due_precision']=='minute' and rows[2]['renewable']

@pytest.mark.parametrize('payload',[{'code':-1},wrap({}),wrap({'data':{'loans':{'showmore':['N']}}}),loans([],more='?')])
def test_failed_or_changed_loan_response_cannot_be_empty(payload):
    with pytest.raises(CampusError):
        lib.parse_loans(payload)

class Browsers:
    def __init__(self,page):
        self.sessions={'id':{'page':page}}
        self.closed=False

    def open(self,**kw):
        return {'session_id':'id','status':'OK'}

    def close(self,sid):
        self.closed=True

def test_expired_saml_does_not_send_private_query():
    b=Browsers(SimpleNamespace(url='https://id.tsinghua.edu.cn/login'))
    with pytest.raises(CampusError,match='AUTH_REQUIRED'):
        lib.read_loans(b)
    assert b.closed

def test_empty_loan_api_requires_matching_official_page():
    node=SimpleNamespace(count=lambda:1,inner_text=lambda:'加载中',all_text_contents=lambda:[])
    b=Browsers(SimpleNamespace(url=lib.LOANS,evaluate=lambda *args:loans([]),locator=lambda *args:node))
    assert not lib.read_loans(b)['complete']
    assert b.closed

def test_database_guide_denial_and_external_links_are_not_opened():
    calls=[]
    def call(op,**params):
        calls.append(op)
        return wrap({'mmsid':'123','databasename':'测试库','offcampus':'<a href="https://partner.example">机构认证</a>',
                     'specialNotice':'需个人账户'})
    entry=lib.database_links(call,{'database_id':'123'})['items'][0]
    assert entry['offcampus_conditions']=='机构认证' and entry['special_notice']=='需个人账户'
    assert calls==['db_detail'] and entry['official_url'].startswith(lib.DATABASES)
    with pytest.raises(CampusError,match='FORBIDDEN'):
        lib.database_links(lambda *a,**kw:{'code':403},{'database_id':'123'})

def test_public_client_blocks_redirect_without_following(monkeypatch):
    reader=lib.PublicReader()
    class Redirect:
        status_code=302
        def __enter__(self):return self
        def __exit__(self,*args):pass
    def request(method,url,**opts):
        assert url==lib.GUIDE_API+'searchDesc' and not opts['allow_redirects']
        assert 'PrimoAuthorization' not in opts['headers']
        return Redirect()
    monkeypatch.setattr(reader.session,'request',request)
    reader.jwt='guest'
    try:
        with pytest.raises(CampusError,match='REDIRECT_BLOCKED'):
            reader.call('db_search',searchKey='abc')
    finally:reader.session.close()

def test_private_cursor_bound_to_account_and_flag_hides_operations(monkeypatch):
    account={'account_id':'one'}
    owner=SimpleNamespace(bridge=lambda *args:account,browsers=None)
    engine=QueryEngine(owner)
    monkeypatch.setattr(lib,'query',lambda *args:lib.result([{'loan_id':'1'},{'loan_id':'2'}],lib.LOANS))
    first_page=engine.query('library','loans',{},page_size=1)
    account['account_id']='two'
    with pytest.raises(CampusError,match='CURSOR_QUERY_MISMATCH'):
        engine.query('library','loans',{},cursor=first_page['next_cursor'],page_size=1)
    monkeypatch.setenv('THU_LIBRARY_READ','0')
    assert {o['operation'] for o in describe('library')['operations']}=={'page','status'}

