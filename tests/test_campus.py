from __future__ import annotations
import json
from pathlib import Path
import time
import pytest
from thu_assistant.bridge import CampusError
from thu_assistant.browser import BrowserManager,read_request,validate_url
from thu_assistant.mcp_stdio import CampusAssistant,McpServer
from learn_assistant.errors import AuthenticationRequired

def test_browser_scope_and_request_boundary():
    assert validate_url('https://seat.lib.tsinghua.edu.cn/')
    for u in ('http://lib.tsinghua.edu.cn/','https://tsinghua.edu.cn.evil/','https://id.tsinghua.edu.cn@evil/','https://lib.tsinghua.edu.cn:8443/'):
        with pytest.raises(CampusError):validate_url(u)
    assert read_request('https://info.tsinghua.edu.cn/b/getAppList','POST','{}')
    assert not read_request('https://info.tsinghua.edu.cn/b/getAppList','POST','{"action":"delete"}')
    assert not read_request('https://x.tsinghua.edu.cn/b/%73ave.do')
    assert not read_request('https://x.tsinghua.edu.cn/api/order','POST','{}')
    assert not read_request('https://x.tsinghua.edu.cn/a?action=cancel')
    assert read_request('https://id.tsinghua.edu.cn/ui/auth/js/saveTabchooseAndInit.js')
    assert read_request('https://thos.tsinghua.edu.cn/fp/fp/formHome/AllSvsByConditionpage','POST','{}')
    assert not read_request('https://thos.tsinghua.edu.cn/fp/fp/formHome/save','POST','{}')
    assert read_request('https://idp.tsinghua.edu.cn/idp/profile/SAML2/POST/SSO','POST','SAMLRequest=synthetic')
    assert not read_request('https://idp.tsinghua.edu.cn/idp/admin/delete','POST','{}')

def test_session_file_change_reloads_long_running_learning_provider(tmp_path,monkeypatch):
    import thu_assistant.mcp_stdio as module
    file=tmp_path/'session.dpapi';file.write_bytes(b'fake-encrypted-one')
    monkeypatch.setattr(module,'SecureSessionStore',lambda:type('Store',(),{'path':file})())
    made=[]
    assistant=CampusAssistant(learning_factory=lambda:made.append(object()) or made[-1])
    first=assistant._learning();assert assistant._learning() is first
    temp=tmp_path/'new';temp.write_bytes(b'fake-encrypted-two-longer');temp.replace(file)
    assert assistant._learning() is not first
    assert len(made)==2

def test_learning_authentication_failure_retries_once_only(tmp_path,monkeypatch):
    import thu_assistant.mcp_stdio as module
    monkeypatch.setattr(module,'SecureSessionStore',lambda:type('Store',(),{'path':tmp_path/'none'})())
    class Expired:
        def call_tool(self,*a):raise AuthenticationRequired('synthetic')
    calls=[]
    assistant=CampusAssistant(learning_factory=Expired,bridge=lambda *a,**k:calls.append(a) or {})
    with pytest.raises(AuthenticationRequired):assistant.call('list_courses',{})
    assert len(calls)==1

class Element:
    def __init__(self):self.count=0
    def click(self,**kwargs):self.count+=1
class Page:
    url='https://thos.tsinghua.edu.cn/service/form'
    def wait_for_load_state(self,*a,**k):pass

def plans(tmp_path):
    from thu_assistant.actions import ActionEngine,ActionStore
    from synthetic_store import make_action_store
    manager=BrowserManager(ActionEngine(make_action_store(tmp_path),account_provider=lambda:'synthetic-account'));element=Element()
    manager.sessions['test']={'revision':1,'refs':{'submit':element},'page':Page(),'secrets':[],'account_id':'synthetic-account'}
    state={'digest':'one'}
    manager._form_state=lambda s,e:({'url':Page.url,'label':'确认预约','fields':[{'name':'date','value':'synthetic-date','type':'text'}]},state['digest'])
    manager.snapshot=lambda sid:{'session_id':sid}
    return manager,element,state

def test_unadapted_action_preview_never_submits(tmp_path):
    m,e,_=plans(tmp_path)
    plan=m.prepare('test',1,'submit','查询后的具体预约')
    assert plan['status']=='PREPARED';assert e.count==0
    with pytest.raises(CampusError):m.execute(plan['plan_id'],'')
    with pytest.raises(CampusError,match='ADAPTER_REQUIRED'):m.execute(plan['plan_id'],'用户明确授权测试中的这笔具体预约')
    assert e.count==0

def test_changed_or_expired_form_cannot_execute(tmp_path):
    m,e,state=plans(tmp_path);plan=m.prepare('test',1,'submit','one')
    state['digest']='different-hidden-resource-id'
    with pytest.raises(CampusError):m.execute(plan['plan_id'],'用户明确授权该具体操作')
    assert e.count==0
    m.actions.clock=lambda:time.time()+601
    assert m.execute(plan['plan_id'],'用户明确授权该具体操作')['reason']=='ACTION_PLAN_EXPIRED'
    assert e.count==0

def test_mcp_rejects_unknown_or_malformed_calls_and_redacts_exception_messages():
    server=McpServer(CampusAssistant(learning_factory=lambda:None,bridge=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('Bearer synthetic-private-value'))))
    def call(name,args):return server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}})['result']
    assert call('model_chat',{'prompt':'x','max_tokens':999999})['isError']
    assert call('not-a-tool',{})['isError']
    error=call('login',{})
    assert error['isError'];assert 'synthetic-private-value' not in json.dumps(error)
    assert call('browser_fill',{'session_id':'x','snapshot_id':True,'element_id':'x','value':'x'})['isError']


def test_browser_persistence_reports_errors_and_rotates_sync_token(monkeypatch):
    import thu_assistant.browser as module
    class Context:
        def storage_state(self):return {'cookies':[],'origins':[]}
    session={'context':Context(),'sync_token':'previous'}
    def success(command,**kwargs):
        assert command=='import_browser_state'
        assert kwargs['state']['syncToken']=='previous'
        assert kwargs['state']['cookies']==[]
        assert kwargs['state']['origins']==[]
        return {'syncToken':'next'}
    monkeypatch.setattr(module,'gateway',success)
    assert BrowserManager._persist_context(session)
    assert session['sync_token']=='next'
    def conflict(*a,**k):raise CampusError('STATE_CONFLICT')
    monkeypatch.setattr(module,'gateway',conflict)
    assert not BrowserManager._persist_context(session)
    assert session['persistence_error']=='STATE_CONFLICT'
    assert session['sync_token']=='next'
    def encrypt_failure(*a,**k):raise CampusError('STATE_ENCRYPT_FAILED')
    monkeypatch.setattr(module,'gateway',encrypt_failure)
    assert not BrowserManager._persist_context(session)
    assert session['persistence_error']=='STATE_ENCRYPT_FAILED'


def test_browser_snapshot_rejects_unprovable_identity(monkeypatch):
    import thu_assistant.browser as module
    manager = BrowserManager.__new__(BrowserManager)
    closed = []
    manager.close = lambda sid: closed.append(sid)
    def failed(*args, **kwargs):
        raise CampusError('STATE_DECRYPT_FAILED')
    monkeypatch.setattr(module, 'gateway', failed)
    with pytest.raises(CampusError, match='STATE_DECRYPT_FAILED'):
        manager._require_account('session-a', {'account_id': 'account-a'})
    assert closed == ['session-a']
