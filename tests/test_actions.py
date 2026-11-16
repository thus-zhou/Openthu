"""Synthetic transactions only. No campus business request is sent by this suite."""
from copy import deepcopy
import json,os,subprocess,sys,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from thu_assistant.actions import ActionEngine,RequestGate,RequestSpec,NotDispatched,CHECKS,process_alive
from thu_assistant.action_store import ActionStore
from synthetic_store import make_action_store
from thu_assistant.bridge import CampusError
from thu_assistant.browser import BrowserManager

BUSINESS=dict(target='synthetic-court',date='2099-01-01',start_time='12:00',end_time='13:00',
    members='synthetic-private-person-123456789012345678',headcount=1,fee='0',payment_method='none')
URL='https://sports.tsinghua.edu.cn/api/reserve'
AUTH='Synthetic human instruction for this exact test transaction'

def consent(r):
    return dict(plan_id=r['operation_id'],account=r['account'],digest=r['digest'],adapter_version=r['adapter_version'],
        source='human',reference='synthetic-host-event',expires_at=time.time()+300)

class Adapter:
    version='test-v1'
    def __init__(self,store):self.store=store;self.sent=0;self.mode='normal';self.remote='SUCCEEDED';self.change=None
    def preview(self,b):return b
    def preflight(self,r):
        b=deepcopy(r['business'])
        if self.change:b[self.change]='changed'
        return dict(business=b,context=r['context'],checks={k:True for k in CHECKS},
            requests=[RequestSpec(URL,'POST',json.dumps(b),frame='0:synthetic')])
    def dispatch(self,r,gate):
        assert self.store.read(r['operation_id'])['state']=='DISPATCHING'
        if self.mode=='before':raise NotDispatched()
        assert gate.consume(URL,'POST',json.dumps(r['business']),frame='0:synthetic')
        self.sent+=1
        if self.mode=='crash':raise SystemExit(7)
        if self.mode=='lost':raise TimeoutError('synthetic-private-token')
    def reconcile(self,r):
        if self.mode=='unavailable':raise RuntimeError('synthetic-private-token')
        row=dict(business=r['business'],account=r['account'],service_id=r['service_id'],operation=r['operation'],record_id='private-record',state=self.remote)
        if self.mode=='foreign':row['account']='different'
        if self.mode=='foreign-service':row['service_id']='different'
        return {'records':[row,row] if self.mode=='duplicate' else [row], 'complete':self.mode!='partial'}

def setup_engine(tmp_path,**kwargs):
    store=make_action_store(tmp_path) # Real DPAPI on Windows; explicit test codec elsewhere.
    adapter=Adapter(store)
    engine=ActionEngine(store,account_provider=lambda:'synthetic-account',adapters={('sports','create'):adapter},consent_verifier=consent,**kwargs)
    return engine,adapter

def plan(engine):return engine.prepare('sports','create',BUSINESS)['plan_id']

def test_atomic_journal_and_safe_public_result(tmp_path):
    e,a=setup_engine(tmp_path);pid=plan(e)
    raw=e.store.path(pid).read_bytes()
    assert b'synthetic-private' not in raw and b'synthetic-account' not in raw
    assert e.store.read(pid)['business']==BUSINESS
    result=e.execute(pid,AUTH)
    assert result['status']=='SUCCEEDED' and result['verification_level']=='REMOTE_RECORD_VERIFIED'
    assert 'synthetic-private' not in json.dumps(result) and 'private-record' not in json.dumps(result)
    assert e.execute(pid,AUTH)['status']=='SUCCEEDED' and a.sent==1
    assert plan(e)==pid # Re-preparing cannot silently duplicate a completed transaction.
    assert not list(tmp_path.glob('*.tmp'))

@pytest.mark.parametrize('mode,status,reason',[
    ('before','FAILED','REQUEST_NOT_SENT'),('lost','SUCCEEDED','REMOTE_RECORD_MATCHED'),
    ('unavailable','UNKNOWN','RECONCILIATION_UNAVAILABLE'),('duplicate','UNKNOWN','DUPLICATE_REMOTE_RECORDS'),
    ('foreign','UNKNOWN','REMOTE_RESULT_NOT_PROVEN'),('foreign-service','UNKNOWN','REMOTE_RESULT_NOT_PROVEN'),
    ('partial','UNKNOWN','REMOTE_RESULT_NOT_PROVEN')])
def test_uncertainty_never_replays(tmp_path,mode,status,reason):
    e,a=setup_engine(tmp_path);pid=plan(e);a.mode=mode
    result=e.execute(pid,AUTH)
    assert (result['status'],result['reason'])==(status,reason)
    before=a.sent;e.execute(pid,AUTH);assert a.sent==before
    if status=='UNKNOWN':assert plan(e)==pid

@pytest.mark.parametrize('state',['PENDING_PAYMENT','PENDING_APPROVAL','FAILED'])
def test_remote_state_not_collapsed_to_success(tmp_path,state):
    e,a=setup_engine(tmp_path);a.remote=state;pid=plan(e)
    assert e.execute(pid,AUTH)['status']==state
    if state!='FAILED':
        a.remote='SUCCEEDED';assert e.result(pid)['status']=='SUCCEEDED'
    assert a.sent==1

def test_host_evidence_required_strings_never_grant_permission(tmp_path):
    e,a=setup_engine(tmp_path);pid=plan(e);e.consent_verifier=lambda r:None
    with pytest.raises(CampusError,match='TRUSTED_AUTHORIZATION'):e.execute(pid,AUTH)
    e.consent_verifier=lambda r:{**consent(r),'digest':'another-operation'}
    with pytest.raises(CampusError,match='TRUSTED_AUTHORIZATION'):e.execute(pid,AUTH)
    assert a.sent==0 and e.store.read(pid)['state']=='PREPARED'

@pytest.mark.parametrize('field',['target','date','start_time','end_time','members','headcount','fee','payment_method'])
def test_every_critical_field_revalidated(tmp_path,field):
    e,a=setup_engine(tmp_path);pid=plan(e);a.change=field
    assert e.execute(pid,AUTH)['reason']=='PRECONDITIONS_CHANGED_PREPARE_AGAIN';assert a.sent==0

def test_expiry_account_adapter_context_and_missing_fields(tmp_path):
    e,a=setup_engine(tmp_path);pid=plan(e)
    e.account_provider=lambda:'other'
    with pytest.raises(CampusError,match='ACCOUNT_MISMATCH'):e.execute(pid,AUTH)
    with pytest.raises(CampusError,match='ACCOUNT_MISMATCH'):e.result(pid)
    e.account_provider=lambda:'synthetic-account';a.version='new'
    with pytest.raises(CampusError,match='ADAPTER_CHANGED'):e.execute(pid,AUTH)
    a.version='test-v1';e.clock=lambda:time.time()+601
    assert e.execute(pid,AUTH)['reason']=='ACTION_PLAN_EXPIRED'
    assert e.prepare('sports','create',{})['status']=='NEEDS_USER_ACTION'
    assert e.prepare('sports','cancel',{'target':'synthetic','record_id':'private'})['reason']=='ADAPTER_REQUIRED'
    assert a.sent==0

def test_page_or_iframe_context_change_invalidates(tmp_path):
    e,a=setup_engine(tmp_path);pid=plan(e);old=a.preflight
    a.preflight=lambda r:{**old(r),'context':{'frame_index':1}}
    assert e.execute(pid,AUTH)['status']=='FAILED' and a.sent==0

def test_concurrent_execute_sends_once(tmp_path):
    e,a=setup_engine(tmp_path);pid=plan(e)
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:e.execute(pid,AUTH),range(4)))
    assert a.sent==1
    assert {r['status'] for r in results}<={'DISPATCHING','SUCCEEDED'}

def test_restarted_engine_only_reconciles_and_owner_probe_is_non_destructive(tmp_path):
    e,a=setup_engine(tmp_path);pid=plan(e);a.mode='crash'
    with pytest.raises(SystemExit):e.execute(pid,AUTH)
    assert process_alive(os.getpid())
    assert e.result(pid)['status']=='DISPATCHING'
    e.store.update(pid,lambda r:r.update(owner_pid=2147483647))
    resumed,b=setup_engine(tmp_path)
    assert resumed.execute(pid,AUTH)['status']=='SUCCEEDED' and b.sent==0 and a.sent==1

def test_actual_child_process_crash_and_recovery(tmp_path):
    e,a=setup_engine(tmp_path);pid=plan(e)
    code='''import os,sys\nfrom pathlib import Path\nfrom test_actions import setup_engine,AUTH\ne,a=setup_engine(Path(sys.argv[1]))\nold=a.dispatch\ndef crash(r,g):\n old(r,g)\n os._exit(7)\na.dispatch=crash\ne.execute(sys.argv[2],AUTH)'''
    env={**os.environ,'PYTHONPATH':os.pathsep.join([str(Path(__file__).parent),str(Path(__file__).parents[1]/'src')])}
    child=subprocess.run([sys.executable,'-c',code,str(tmp_path),pid],env=env,timeout=20,capture_output=True)
    assert child.returncode==7
    assert e.store.read(pid)['state']=='DISPATCHING'
    assert e.execute(pid,AUTH)['status']=='SUCCEEDED' and a.sent==0

def test_two_real_processes_claim_once(tmp_path):
    e,a=setup_engine(tmp_path);pid=plan(e)
    code='''import sys,time\nfrom pathlib import Path\nfrom test_actions import setup_engine,AUTH\ne,a=setup_engine(Path(sys.argv[1]))\nold=a.dispatch\ndef dispatch(r,g):\n old(r,g)\n with open(Path(sys.argv[1])/'sent.txt','a') as f:f.write('sent\\n')\n time.sleep(.2)\na.dispatch=dispatch\nprint('ready',flush=True)\ninput()\ne.execute(sys.argv[2],AUTH)'''
    env={**os.environ,'PYTHONPATH':os.pathsep.join([str(Path(__file__).parent),str(Path(__file__).parents[1]/'src')])}
    children=[subprocess.Popen([sys.executable,'-c',code,str(tmp_path),pid],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
    try:
        for child in children:assert child.stdout.readline().strip()=='ready'
        for child in children:child.stdin.write('\n');child.stdin.flush()
        for child in children:assert child.wait(timeout=15)==0
        assert (tmp_path/'sent.txt').read_text().splitlines()==['sent']
    finally:
        for child in children:
            if child.poll() is None:child.kill();child.wait(timeout=5)

@pytest.mark.parametrize('check',CHECKS)
def test_preflight_qualification_rules_and_fee_fail_closed(tmp_path,check):
    e,a=setup_engine(tmp_path);pid=plan(e);old=a.preflight
    def failed(r):
        result=old(r);result['checks'][check]=False;return result
    a.preflight=failed
    assert e.execute(pid,AUTH)['status']=='FAILED' and a.sent==0

def test_failure_saving_after_send_retains_dispatch_journal(tmp_path):
    e,a=setup_engine(tmp_path);pid=plan(e);old=a.dispatch;protect=e.store.protect
    def fail_save(r,g):old(r,g);e.store.protect=lambda b:None
    a.dispatch=fail_save
    with pytest.raises(CampusError,match='ENCRYPT_FAILED'):e.execute(pid,AUTH)
    assert a.sent==1 and e.store.read(pid)['state']=='DISPATCHING'
    e.store.protect=protect;e.store.update(pid,lambda r:r.update(owner_pid=2147483647))
    assert e.result(pid)['status']=='SUCCEEDED' and a.sent==1

def test_offline_rollback_inventory_preserves_pending_without_values(tmp_path):
    from thu_assistant.action_recovery import inventory
    e,a=setup_engine(tmp_path);pid=plan(e);a.mode='unavailable';e.execute(pid,AUTH)
    before=e.store.path(pid).read_bytes();out=inventory(e.store)
    assert out['operations'][0]['state']=='UNKNOWN'
    assert 'synthetic-private' not in json.dumps(out) and 'synthetic-account' not in json.dumps(out)
    assert before==e.store.path(pid).read_bytes() and out['dispatched'] is False

def test_ordered_gate_never_skips_or_replays_a_step():
    one=RequestSpec(URL,'POST','x=1','frame','form');two=RequestSpec(URL+'/confirm','POST','x=2','frame','form')
    g=RequestGate([one,two])
    assert not g.consume(two.url,two.method,two.body,two.frame)
    assert g.consume(one.url,one.method,one.body,one.frame)
    assert not g.consume(one.url,one.method,one.body,one.frame)
    assert g.consume(two.url,two.method,two.body,two.frame)

def test_failed_encryption_or_corrupt_log_never_dispatches(tmp_path):
    e,a=setup_engine(tmp_path);pid=plan(e);e.store.protect=lambda b:None
    with pytest.raises(CampusError,match='ENCRYPT_FAILED'):e.execute(pid,AUTH)
    assert a.sent==0 and e.store.read(pid)['state']=='PREPARED'
    e.store.path(pid).write_bytes(b'corrupt')
    with pytest.raises(CampusError,match='CORRUPT'):e.execute(pid,AUTH)
    with pytest.raises(CampusError,match='INVALID_OPERATION_ID'):e.result('../secret')

def test_gate_exact_request_sequence_and_frame():
    g=RequestGate([RequestSpec(URL,'POST','{"target":1,"csrf":"fresh"}',frame='1:frame')])
    for url,method,body,frame in [(URL,'POST','{"target":2,"csrf":"fresh"}','1:frame'),(URL,'POST','{"target":1,"csrf":"fresh"}','0:frame'),(URL+'/other','POST','{}','1:frame'),(URL,'DELETE',None,'1:frame'),(URL,'POST','{"target":2,"target":1,"csrf":"fresh"}','1:frame')]:
        assert not g.consume(url,method,body,frame)
    assert g.consume(URL,'POST','{"csrf":"fresh","target":1}','1:frame')
    assert not g.consume(URL,'POST','{"csrf":"fresh","target":1}','1:frame')
    g.close();assert not g.consume(URL,'POST','{}','1:frame')

def test_browser_route_denies_unrelated_same_origin_writes():
    class Frame:url='https://sports.tsinghua.edu.cn/form'
    f=Frame()
    class Page:frames=[f]
    class Request:
        frame=f;url=URL;method='POST';post_data='{}'
    g=RequestGate([RequestSpec(URL,'POST','{}','0:'+f.url)])
    s={'page':Page(),'request_gate':g};r=Request()
    r.url=URL+'/other';assert not BrowserManager.request_allowed(s,r)
    r.url='https://sports.tsinghua.edu.cn/getOrder';assert not BrowserManager.request_allowed(s,r)
    r.url=URL;assert BrowserManager.request_allowed(s,r)
    assert not BrowserManager.request_allowed(s,r)

def test_real_browser_controls_iframe_and_csrf_exclusion(tmp_path):
    from playwright.sync_api import sync_playwright
    e,a=setup_engine(tmp_path);manager=BrowserManager(e)
    markup='''<form><input name="csrf" value="temporary-token"><input type="hidden" name="resource" value="court-a">
      <input name="phone" value="private-phone"><input type="checkbox" name="agree" value="yes" checked>
      <input type="radio" name="time" value="a"><input type="radio" name="time" value="b" checked>
      <select name="members" multiple><option value="a" selected>A</option><option value="b" selected>B</option></select>
      <div role="option" aria-selected="true" data-value="court-a">Court</div><button type="submit">Submit</button></form>'''
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda route:route.fulfill(body='<iframe src="https://sports.tsinghua.edu.cn/inner"></iframe>' if route.request.url.endswith('/outer') else markup,content_type='text/html'))
            page.goto('https://sports.tsinghua.edu.cn/outer');frame=page.frames[1];button=frame.locator('button');button.wait_for()
            s={'page':page,'revision':1,'refs':{'f1':button},'account_id':'synthetic-account','secrets':[]};manager.sessions['s']=s
            state,first=manager._form_state(s,button)
            assert state['frameIndex']==1 and all(f['name']!='csrf' for f in state['fields'])
            assert [f['checked'] for f in state['fields'] if f['type']=='radio']==[False,True]
            assert next(f for f in state['fields'] if f['name']=='members')['selected']==['a','b']
            assert any(f['ariaSelected']=='true' for f in state['fields'])
            frame.locator('[name=csrf]').fill('different-token');assert manager._form_state(s,button)[1]==first
            frame.locator('[name=agree]').uncheck();assert manager._form_state(s,button)[1]!=first
            preview=manager.prepare('s',1,'f1','synthetic')
            assert 'private-phone' not in json.dumps(preview) and preview['reason']=='ADAPTER_REQUIRED'
            with pytest.raises(CampusError,match='ADAPTER_REQUIRED'):manager.execute(preview['plan_id'],AUTH)
            assert a.sent==0
        finally:browser.close()
