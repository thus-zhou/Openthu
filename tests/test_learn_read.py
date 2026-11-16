from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import base64,json,hashlib
import pytest
from learn_assistant.providers.learn_read import LearnReadProvider,create_read_provider
from learn_assistant.providers.learn_read_parsers import parse_notice,parse_jsonp,attachment_links
from learn_assistant.providers.web import WebProvider
from learn_assistant.models import BrowserCookie,BrowserSession,Course
from learn_assistant.core import LearnAssistant
from learn_assistant.errors import ValidationError,InterfaceDriftError
from thu_assistant.mcp_stdio import CampusAssistant
from test_web_provider import FakeResponse

def notice(n=1):return {'ggid':f'n{n}','bt':'公告 &amp; 标题','ggnr':base64.b64encode(b'<p>Hello <b>world</b></p><script>secret()</script>').decode(),'fbsj':'2026-10-31','fjmc':'same.txt'}
HTML='''<a class="ml-10" href="/b/wlxt/kc/wj_wjb/downloadFile?wjid=f1">same.txt</a>
<a class="ml-10" href="/b/wlxt/kc/wj_wjb/downloadFile?wjid=f2">same.txt</a>'''

class Transport:
    def __init__(self):self.calls=[];self.notices=[notice()];self.repeat=False;self.fail_course=None;self.calendar_rows=[];self.calendar_bad=False
    def request(self,method,url,**kw):
        self.calls.append((method,url,kw));u=urlsplit(url);q=parse_qs(u.query);path=u.path
        def data(value):return FakeResponse(json_data=value,url=url)
        def ok(value):return data({'result':'success','object':value})
        if path.endswith('/queryxnxq'):return data([None,'2026-2027-1','2025-2026-2'])
        if path.endswith('/getCurrentAndNextSemester'):return data({'message':'success','result':{'xnxq':'2026-2027-1'}})
        if '/loadCourseBySemesterId/' in path:return data({'message':'success','resultList':[{'wlkcid':'c1','kcm':'same'},{'wlkcid':'c2','kcm':'same'}]})
        if 'pageListXsby' in path or '/zyList' in path:
            ao={v['name']:v['value'] for v in json.loads(kw['data']['aoData'])}
            if ao['wlkcid']==self.fail_course:return FakeResponse(status_code=403,url=url)
            if 'Wgq' in path:rows=self.notices
            elif 'Ygq' in path:rows=[notice(999)]
            elif 'Wj' in path:rows=[{'xszyid':'a1','zyid':'base1','wlkcid':ao['wlkcid'],'bt':'Late','jzsj':'2000-01-01 10:00:00','cj':None}]
            elif 'Yjwg' in path:rows=[{'xszyid':'a2','zyid':'base2','wlkcid':ao['wlkcid'],'bt':'Pending','jzsj':'2000-01-01','cj':None}]
            else:rows=[{'xszyid':'a3','zyid':'base3','wlkcid':ao['wlkcid'],'cj':0,'pynr':'feedback','pysj':'2026-10-31'}]
            start=0 if self.repeat else ao['iDisplayStart']
            return ok({'iTotalDisplayRecords':str(len(rows)),'aaData':rows[start:start+ao['iDisplayLength']]})
        if path.endswith('/beforeViewXs') or path.endswith('/viewCj'):return FakeResponse(body=HTML.encode(),url=url)
        if path.endswith('/student/detail'):return data({'result':'success','msg':'<p>Instructions</p>'})
        if path.endswith('kjflb/student/pageList'):return ok({'records':1,'rows':[{'kjflid':'cat','bt':'docs'}]})
        if '/student/kjxxb/' in path:
            rows=[]
            for n in range(230):
                row=[None]*15;row[0]='k'+str(n);row[1]='file'+str(n);row[7]='f'+str(n);row[9]=3;row[13]='txt';rows.append(row)
            return ok(rows)
        if path.endswith('/kjxxbByWlkcidAndSizeForStudent'):return ok([])
        if path.endswith('/downloadFile'):return FakeResponse(body=('content-'+q['wjid'][0]).encode(),url=url)
        if path.endswith('/auth/gnt'):return FakeResponse(body=b'SYNTHETIC_OPAQUE_TICKET_1234567890',url=url)
        if path.endswith('/j_acegi_login.do'):return FakeResponse(body=b'<html>OK</html>',url=url)
        if path.endswith('/jxmh_out.do'):
            if self.calendar_bad and q['p_start_date'][0]>'20261028':return FakeResponse(status_code=503,url=url)
            return FakeResponse(body=('thuCalendar('+json.dumps(self.calendar_rows)+');').encode(),url=url)
        return FakeResponse(status_code=404,url=url)

def provider():
    t=Transport();s=BrowserSession((BrowserCookie('test','synthetic',domain='learn.tsinghua.edu.cn'),),csrf_token='private-csrf')
    return LearnReadProvider(transport=t,browser_session=s,load_persisted=False),t

def test_semesters_null_and_duplicate_name_requires_choice():
    p,t=provider();r=p.query_learning('semesters',{})
    assert len(r['items'])==2 and r['items'][0]['current']
    r=p.query_learning('notices',{'course_name':'same','semester':'2025-2026-2'})
    assert r['reason']=='AMBIGUOUS_COURSE' and len(r['candidates'])==2
    assert all(c['semester']=='2025-2026-2' for c in r['candidates'])
    assert p.query_learning('notices',{'course_name':'absent'})['reason']=='COURSE_NOT_FOUND'
    assert p.query_learning('notices',{'course_id':'c1','course_name':'same'})['complete']

def test_notice_paging_both_categories_safe_html_and_details():
    p,t=provider();t.notices=[notice(i) for i in range(205)]
    r=p.query_learning('notices',{'course_id':'c1'})
    assert len(r['items'])==206 and r['complete'];assert len(r['pages'])==4
    item=r['items'][0];assert item['title']=='公告 & 标题' and 'secret' not in item['content_text'] and '<b>' in item['content_html']
    assert item['attachments_state']=='NOT_READ' and r['items'][-1]['expired']
    d=p.query_learning('notice_detail',{'course_id':'c1','notice_id':item['id']})
    assert len(d['items'][0]['attachments'])==2
    assert all(a['download_state']=='NOT_DOWNLOADED' for a in d['items'][0]['attachments'])
    assert not any('/downloadFile?' in u for _,u,_ in t.calls)

def test_repeated_upstream_page_never_looks_complete():
    p,t=provider();t.notices=[notice(i) for i in range(205)];t.repeat=True
    r=p.query_learning('notices',{'course_id':'c1'})
    assert not r['complete'] and 'REPEATED_UPSTREAM_PAGE' in r['warnings']
    assert len(r['items'])==101

def test_partial_course_error_preserves_other_courses_and_is_not_empty():
    p,t=provider();t.fail_course='c2';r=p.query_learning('notices',{})
    assert len(r['items'])==2 and not r['complete'] and len(r['errors'])==2
    assert {e['code'] for e in r['errors']}=={'PermissionError'}

def test_assignment_unknown_score_zero_score_feedback_and_overdue():
    p,t=provider();r=p.query_learning('assignments',{'course_id':'c1'});a,b,c=r['items']
    assert a['overdue'] and not a['submitted'] and a['grade'] is None
    assert b['submitted'] and not b['graded'] and b['grade'] is None and not b['overdue']
    assert c['graded'] and c['grade']==0 and c['feedback']=='feedback'
    d=p.query_learning('assignment_detail',{'course_id':'c1','assignment_id':'a1'})
    assert len(d['items'][0]['attachments'])==2 and 'Instructions' in d['items'][0]['instructions']

def test_more_than_200_files_use_uncapped_category_and_old_api_fields():
    p,t=provider();a=LearnAssistant(p);r=a.list_course_files(course_id='c1')
    assert r['count']==230 and r['read_evidence']['complete'] and r['files'][0]['relative_path']=='docs/file0'

def test_attachment_same_name_never_overwrites_and_can_rehydrate(tmp_path):
    p,t=provider();r=p.query_learning('notice_detail',{'course_id':'c1','notice_id':'n1'});ids=[a['file_id'] for a in r['items'][0]['attachments']]
    fresh,_=provider();a=LearnAssistant(fresh);out=a.download_files(file_ids=ids,destination=tmp_path)
    assert out['ok'] and len(out['downloaded'])==2
    assert len({x['path'] for x in out['downloaded']})==2
    for d in out['downloaded']:
        assert hashlib.sha256(Path(d['path']).read_bytes()).hexdigest()==d['sha256'];assert d['source_kind']=='notice' and d['source_id']=='n1'
    assert len(a.download_files(file_ids=ids,destination=tmp_path)['skipped'])==2

def test_calendar_cross_month_dedupe_by_id_and_no_cross_origin_csrf():
    p,t=provider();t.calendar_rows=[{'id':'event1','nq':'2026-10-28','kssj':'8:00','jssj':'9:00','nr':'Test','dd':'Room','fl':'1'}]
    r=p.query_learning('calendar',{'start_date':'2026-10-01','end_date':'2026-11-05'})
    assert r['complete'] and len(r['items'])==1 and len(r['pages'])==2
    for method,url,kw in t.calls:
        if urlsplit(url).hostname=='zhjw.cic.tsinghua.edu.cn':assert '_csrf' not in url and 'X-XSRF-TOKEN' not in kw['headers']
    assert 'SYNTHETIC_OPAQUE' not in json.dumps(r)

def test_calendar_missing_id_and_partial_window_are_explicit():
    p,t=provider();t.calendar_rows=[{'nq':'2026-10-28','kssj':'8','jssj':'9','nr':'Test'}];t.calendar_bad=True
    r=p.query_learning('calendar',{'start_date':'2026-10-01','end_date':'2026-11-05'})
    assert not r['complete'] and len(r['items'])==1 and 'EVENT_ID_UNAVAILABLE_NO_DEDUP' in r['warnings']

@pytest.mark.parametrize('start,end',[('x','2026-10-31'),('2026-02-30','2026-10-31'),('2026-10-31','2026-01-01'),('2024-01-01','2026-01-01')])
def test_invalid_calendar_range_never_authenticates(start,end):
    p,t=provider()
    with pytest.raises(ValidationError):p.query_learning('calendar',{'start_date':start,'end_date':end})
    assert not t.calls

@pytest.mark.parametrize('value',['callback([]);evil()','thuCalendar({})','thuCalendar(null)','<html>login</html>'])
def test_jsonp_never_executes_code(value):
    with pytest.raises(InterfaceDriftError):parse_jsonp(value)

@pytest.mark.parametrize('url',['https://evil.test/downloadFile?wjid=f1','/b/wlxt/delete?wjid=f1','https://learn.tsinghua.edu.cn:8443/b/wlxt/kc/wj_wjb/downloadFile?wjid=f1'])
def test_attachment_link_does_not_register_untrusted_target(url):
    assert attachment_links('<a href="'+url+'">file</a>','c1','notice','n1')==[]

def test_query_schema_paging_and_legacy_tool_contracts():
    p,t=provider();a=CampusAssistant(learning_factory=lambda:LearnAssistant(p))
    r=a.call('query_service',{'service_id':'learn','operation':'notices','parameters':{'course_id':'c1'},'page_size':1})
    assert r['next_cursor'] and not r['complete'] and r['source_complete']
    r=a.call('query_service',{'service_id':'learn','operation':'notices','parameters':{'course_id':'c1'},'page_size':1,'cursor':r['next_cursor']})
    assert r['complete']
    assert a.call('list_assignments',{'course_id':'c1'})['status']=='OK'
    assert a.call('list_course_files',{'course_id':'c1'})['count']==230
    with pytest.raises(Exception):a.call('query_service',{'service_id':'learn','operation':'calendar','parameters':{'start_date':'2026-10-01','end_date':'2026-11-01','url':'https://evil.test'}})

def test_read_feature_flag_falls_back_without_changing_credentials(monkeypatch):
    monkeypatch.setenv('THU_LEARN_READ_V2','0');assert type(create_read_provider()) is WebProvider
    monkeypatch.setenv('THU_LEARN_READ_V2','1');assert type(create_read_provider()) is LearnReadProvider

def test_vendor_exact_commit_and_hashes():
    root=Path(__file__).parents[1]/'src/learn_assistant/vendor/thu-learn-lib';p=json.loads((root/'PROVENANCE.json').read_text())
    assert p['commit']=='3e8b5ba1382c2c87e9bdba431705dca82d6d07ee' and p['license']=='MIT'
    assert hashlib.sha256((root/'LICENSE').read_bytes()).hexdigest()==p['license_sha256']
    for f in p['files']:assert hashlib.sha256((root/f['excerpt']).read_bytes()).hexdigest()==f['excerpt_sha256']

def test_nested_homework_download_url_preserves_course_binding():
    from urllib.parse import urlencode
    href='/f/wlxt/kc/wj_wjb/student/openNewWindow?'+urlencode({'fileId':'f1','downloadUrl':'/b/wlxt/kczy/zy/student/downloadFile/c1/f1'})
    items=attachment_links('<a href="'+href+'">work.txt</a>','c1','assignment','a1')
    assert len(items)==1 and items[0]['path']=='/b/wlxt/kczy/zy/student/downloadFile/c1/f1'
    assert attachment_links('<a href="'+href+'">work.txt</a>','c2','assignment','a1')==[]

def test_missing_attachment_and_body_encoding_never_claim_full_read():
    p,t=provider();original=t.request
    def missing(method,url,**kwargs):
        if '/beforeViewXs' in url:return FakeResponse(body=b'<html>Changed page</html>',url=url)
        return original(method,url,**kwargs)
    t.request=missing;r=p.query_learning('notice_detail',{'course_id':'c1','notice_id':'n1'})
    assert not r['complete'] and r['items'][0]['attachments_state']=='UNRESOLVED'
    with pytest.raises(InterfaceDriftError):parse_notice({**notice(),'ggnr':'not base64!!'},'c1',False)

def test_missing_total_is_unknown_and_true_empty_requires_both_categories():
    p,t=provider();original=t.request
    def empty(method,url,**kwargs):
        r=original(method,url,**kwargs)
        if 'pageListXsby' in url:r._json_data['object']={'aaData':[],'iTotalDisplayRecords':0}
        return r
    t.request=empty;a=CampusAssistant(learning_factory=lambda:LearnAssistant(p))
    args={'service_id':'learn','operation':'notices','parameters':{'course_id':'c1'}}
    assert a.call('query_service',args)['status']=='EMPTY'
    def unknown(method,url,**kwargs):
        r=empty(method,url,**kwargs)
        if 'pageListXsby' in url:r._json_data['object'].pop('iTotalDisplayRecords')
        return r
    t.request=unknown;r=a.call('query_service',args)
    assert r['status']=='PARTIAL' and not r['complete']

def test_later_page_failure_retains_rows_and_error_not_as_empty():
    p,t=provider();t.notices=[notice(n) for n in range(205)];original=t.request
    def failed(method,url,**kwargs):
        if 'Wgq' in url:
            ao={x['name']:x['value'] for x in json.loads(kwargs['data']['aoData'])}
            if ao['iDisplayStart']>0:return FakeResponse(status_code=403,url=url)
        return original(method,url,**kwargs)
    t.request=failed;r=p.query_learning('notices',{'course_id':'c1'})
    assert len(r['items'])==101 and not r['complete'] and r['errors']

def test_unverified_category_and_200_main_files_remain_partial():
    p,t=provider();original=t.request
    def capped(method,url,**kwargs):
        r=original(method,url,**kwargs)
        if 'kjflb/student/pageList' in url:r._json_data['object'].pop('records')
        if 'kjxxbByWlkcidAndSizeForStudent' in url:r._json_data['object']=[{'wjid':'f'+str(n),'bt':'file'+str(n)} for n in range(200)]
        return r
    t.request=capped;r=p.query_learning('files',{'course_id':'c1'})
    assert len(r['items'])==230 and not r['complete']
    assert 'UNCATEGORIZED_FILES_COMPLETENESS_UNKNOWN' in r['warnings']

@pytest.mark.parametrize('field,value',[('course_id','../../delete'),('semester','a?token=secret')])
def test_identifiers_cannot_change_endpoint(field,value):
    p,t=provider()
    with pytest.raises(ValidationError):p.query_learning('notices',{field:value})
    assert not t.calls

def test_failed_detail_returns_no_fabricated_body():
    p,t=provider();original=t.request
    def failed(method,url,**kwargs):
        if '/student/detail' in url:return FakeResponse(json_data={'result':'fail','msg':'secret'},url=url)
        return original(method,url,**kwargs)
    t.request=failed;r=p.query_learning('assignment_detail',{'course_id':'c1','assignment_id':'a1'})
    assert not r['complete'] and not r['items'] and 'secret' not in json.dumps(r)
