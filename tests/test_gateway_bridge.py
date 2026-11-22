from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import queue
import sys
import pytest
from thu_assistant.bridge import GatewayClient, CampusError

ROOT = Path(__file__).parent
NODE = os.environ.get('THU_NODE', 'node')
GATEWAY = ROOT.parent / 'src/thu_assistant/gateway.js'


@pytest.fixture
def client():
    value = GatewayClient([NODE, str(ROOT / 'fixtures/gateway-worker.js')])
    yield value
    value.close()


def test_concurrent_ids_share_child(client):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: client.call('echo', value=n, delay=5), range(16)))
    assert [r['value'] for r in results] == list(range(16))
    assert len({r['pid'] for r in results}) == 1
    assert client.starts == 1


def test_crash_no_replay_next_call_restarts(client, tmp_path):
    journal = tmp_path / 'journal'
    with pytest.raises(CampusError, match='GATEWAY_EXITED'):
        client.call('crash', journal=str(journal))
    assert client.call('echo', value='fresh')['value'] == 'fresh'
    assert client.starts == 2
    assert journal.read_text().splitlines() == ['crash']
    assert not client.pending


def test_queued_cancel_never_executes(client, tmp_path):
    running = threading.Event()
    cancel = threading.Event()
    journal = tmp_path / 'journal'
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.call, 'model', delay=600, progress=lambda s: running.set() if s == 'RUNNING' else None)
        assert running.wait(5)
        second = pool.submit(client.call, 'write', cancel_event=cancel, journal=str(journal))
        time.sleep(.1)
        cancel.set()
        with pytest.raises(CampusError, match='CANCELLED'):
            second.result(timeout=5)
        first.result(timeout=5)
    assert not journal.exists()
    assert not client.pending


def test_running_timeout_does_not_replay(client, tmp_path):
    client.call('echo')  # Separate process startup time from the task timeout.
    journal = tmp_path / 'journal'
    with pytest.raises(CampusError, match='CAMPUS_GATEWAY_TIMEOUT'):
        client.call('model', delay=1000, timeout=.15, journal=str(journal))
    assert client.call('echo', value=2)['value'] == 2
    assert journal.read_text().splitlines() == ['model']


def test_close_only_owned_child(client):
    client.call('echo')
    proc = client.process
    client.close()
    assert proc.poll() is not None
    with pytest.raises(CampusError, match='GATEWAY_EXITED'):
        client.call('echo')


def test_process_exit_fails_all_waiters_without_replay(client, tmp_path):
    started = threading.Event()
    queued = threading.Event()
    journal = tmp_path / 'journal'
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.call, 'model', delay=5000, progress=lambda s: started.set() if s == 'RUNNING' else None)
        assert started.wait(5)
        second = pool.submit(client.call, 'write', journal=str(journal), progress=lambda s: queued.set() if s == 'QUEUED' else None)
        assert queued.wait(5)
        client.process.kill()
        for future in (first, second):
            with pytest.raises(CampusError, match='GATEWAY_EXITED'):
                future.result(timeout=5)
    assert not journal.exists()
    assert client.call('echo', value='next')['value'] == 'next'
    assert client.starts == 2


def test_queued_timeout_never_executes(client, tmp_path):
    started = threading.Event()
    journal = tmp_path / 'journal'
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.call, 'model', delay=500, progress=lambda s: started.set() if s == 'RUNNING' else None)
        assert started.wait(5)
        with pytest.raises(CampusError, match='QUEUE_TIMEOUT'):
            client.call('write', timeout=.1, journal=str(journal))
        first.result(timeout=5)
    assert not journal.exists()


@pytest.fixture
def real_gateway_env(tmp_path):
    fixture = ROOT / 'fixtures/gateway'
    state = tmp_path / 'state'
    state.mkdir()
    env = {**os.environ, 'THU_ACCESS_HELPER': str(fixture / 'helper.js'), 'THU_MADMODEL_DIR': str(fixture),
           'THU_ASSISTANT_STATE_DIR': str(state / 'campus'), 'MADMODEL_STATE_DIR': str(state),
           'THU_SESSION_ALLOW_LEGACY': '1',
           'THU_TEST_LOCK_MODULE': str(ROOT.parent / 'vendor/madmodel-proxy/platform/process-lock.js')}
    return env, state


def test_two_processes_refresh_once_and_model_releases_auth(real_gateway_env):
    env, state = real_gateway_env
    env['THU_TEST_MODEL_DELAY'] = '700'
    a = GatewayClient([NODE, str(GATEWAY), '--stdio'], env)
    b = GatewayClient([NODE, str(GATEWAY), '--stdio'], env)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(a.call, 'model', prompt='synthetic')
            deadline = time.monotonic() + 5
            while not (state / 'model-started').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert (state / 'model-started').exists()
            # A third process, using the original deployed lock implementation,
            # can acquire AUTH_LOCK while the model task still owns the session lock.
            probe = "const l=require(process.env.THU_TEST_LOCK_MODULE);const p=require('path').join(process.env.MADMODEL_STATE_DIR,'auth.lock');l.acquirePidLock(p).then(x=>{process.stdout.write(JSON.stringify(x));if(x.ok)l.releasePidLock(p);});"
            checked = subprocess.run([NODE, '-e', probe], env=env, capture_output=True, text=True, timeout=5)
            assert json.loads(checked.stdout)['ok']
            second = pool.submit(b.call, 'model', prompt='synthetic')
            assert first.result(timeout=10)['status'] == 'GENERATION_VERIFIED'
            assert second.result(timeout=10)['status'] == 'GENERATION_VERIFIED'
        assert (state / 'refresh-count').read_text() == '1'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda c: c.call('read', url='https://info.tsinghua.edu.cn/', via_webvpn=True), [a,b]))
        assert all(r['status'] == 'PAGE_READ_VERIFIED' for r in results)
        assert (state / 'identity-count').read_text() == '1'
        assert json.loads(__import__('base64').b64decode((state / 'campus/campus-v2.dpapi').read_text()))['version'] == 2
    finally:
        a.close(); b.close()


def test_mfa_not_repeated(real_gateway_env):
    env, state = real_gateway_env
    env['THU_TEST_MFA'] = '1'
    client = GatewayClient([NODE, str(GATEWAY), '--stdio'], env)
    try:
        for _ in range(3):
            with pytest.raises(CampusError, match='TWO_FACTOR_REQUIRED'):
                client.call('read', url='https://info.tsinghua.edu.cn/', via_webvpn=True)
        assert (state / 'identity-count').read_text() == '1'
    finally:
        client.close()


def test_mcp_cancel_notification_and_unchanged_tools(real_gateway_env):
    env, state = real_gateway_env
    env['THU_TEST_MODEL_DELAY'] = '5000'
    env['PYTHONPATH'] = str(ROOT.parent / 'src')
    proc = subprocess.Popen([sys.executable, '-m', 'thu_assistant.mcp_stdio'], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
    output = queue.Queue()
    def collect():
        for line in proc.stdout:
            output.put(json.loads(line))
    threading.Thread(target=collect, daemon=True).start()
    def send(value):
        proc.stdin.write(json.dumps({'jsonrpc':'2.0', **value}) + '\n'); proc.stdin.flush()
    try:
        send({'id':1,'method':'tools/call','params':{'name':'model_chat','arguments':{'prompt':'synthetic'},'_meta':{'progressToken':'test'}}})
        deadline = time.monotonic() + 8
        while not (state / 'model-started').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (state / 'model-started').exists()
        send({'method':'notifications/cancelled','params':{'requestId':1}})
        send({'id':2,'method':'tools/list','params':{}})
        received = {}
        saw_progress = False
        while len(received) < 2:
            message = output.get(timeout=8)
            if message.get('method') == 'notifications/progress':
                saw_progress = True
            elif 'id' in message:
                received[message['id']] = message['result']
        assert saw_progress
        assert received[1]['structuredContent']['error']['code'] == 'CANCELLED'
        assert received[1]['structuredContent']['usage'] is None
        assert received[1]['structuredContent']['usage_state'] == 'UNKNOWN'
        assert received[1]['structuredContent']['generation_started'] is True
        assert (state/'model-count').read_text() == '1'
        assert len(received[2]['tools']) == 29
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait(timeout=5)


def test_auth_busy_keeps_live_owner_and_reports_wait(real_gateway_env):
    env, state = real_gateway_env
    # Hold the original lock in another live process. A short caller deadline
    # cancels waiting without removing that process's file or participant.
    holder_code = "const l=require(process.env.THU_TEST_LOCK_MODULE),p=require('path').join(process.env.MADMODEL_STATE_DIR,'auth.lock');l.acquirePidLock(p).then(x=>{process.stdout.write(JSON.stringify(x)+'\\n');process.stdin.resume();process.stdin.on('end',()=>{l.releasePidLock(p);process.exit();});});"
    holder = subprocess.Popen([NODE,'-e',holder_code],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    client = GatewayClient([NODE,str(GATEWAY),'--stdio'],env)
    statuses = []
    try:
        assert json.loads(holder.stdout.readline())['ok']
        with pytest.raises(CampusError, match='CAMPUS_GATEWAY_TIMEOUT'):
            client.call('model',prompt='synthetic',timeout=.7,progress=statuses.append)
        assert 'WAITING_FOR_AUTH' in statuses
        assert (state / 'auth.lock').read_text() == str(holder.pid)
        assert holder.poll() is None
    finally:
        client.close();holder.stdin.close();holder.wait(timeout=5)


@pytest.mark.parametrize('mode,calls,refreshes,error', [
    ('401once',2,2,None), ('401always',2,2,'MODEL_AUTH_REJECTED'),
    ('started401',1,1,'MODEL_AUTH_REJECTED'), ('403',1,1,'MODEL_FORBIDDEN'),
    ('429',1,1,'MODEL_RATE_LIMITED'), ('network',1,1,'MODEL_NETWORK_ERROR'),
])
def test_model_auth_recovery_does_not_replay_output_or_other_errors(real_gateway_env,mode,calls,refreshes,error):
    env,state=real_gateway_env
    env['THU_TEST_MODEL_MODE']=mode
    client=GatewayClient([NODE,str(GATEWAY),'--stdio'],env)
    try:
        if error:
            with pytest.raises(CampusError,match=error):client.call('model',prompt='synthetic')
        else:
            result=client.call('model',prompt='synthetic')
            assert result['refreshed'] is True
            assert result['status']=='GENERATION_VERIFIED'
        assert int((state/'model-count').read_text())==calls
        assert int((state/'refresh-count').read_text())==refreshes
    finally:client.close()


def test_bad_password_is_latched_without_repeated_login(real_gateway_env):
    env,state=real_gateway_env
    env['THU_TEST_BAD_PASSWORD']='1'
    client=GatewayClient([NODE,str(GATEWAY),'--stdio'],env)
    try:
        for _ in range(3):
            with pytest.raises(CampusError,match='BAD_CREDENTIALS'):
                client.call('read',url='https://info.tsinghua.edu.cn/',via_webvpn=True)
        assert (state/'identity-count').read_text()=='1'
    finally:client.close()


def test_public_read_does_not_send_stored_cookies_or_authenticate(real_gateway_env):
    env,state=real_gateway_env
    client=GatewayClient([NODE,str(GATEWAY),'--stdio'],env)
    try:
        client.call('read',url='https://info.tsinghua.edu.cn/',via_webvpn=True)
        result=client.call('read',url='https://info.tsinghua.edu.cn/public',authenticate=False)
        assert result['public_read'] is True
        assert (state/'identity-count').read_text()=='1'
    finally:client.close()


@pytest.mark.parametrize('values,expected',[(('a=1','b=2'),'saved'),(('a=1','a=2'),'STATE_CONFLICT')])
def test_two_actual_processes_merge_or_reject_stale_state(real_gateway_env,values,expected):
    env,state=real_gateway_env
    workers=[subprocess.Popen([NODE,str(ROOT/'fixtures/session-writer.js')],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True) for _ in range(2)]
    try:
        assert [p.stdout.readline().strip() for p in workers]==['ready','ready']
        for p,value,result in zip(workers,values,('saved',expected)):
            p.stdin.write(value+'\n');p.stdin.flush()
            assert p.stdout.readline().strip()==result
            p.wait(timeout=5)
        saved=json.loads(__import__('base64').b64decode((state/'campus/campus-v2.dpapi').read_text()))
        assert len(saved['cookies'])==(2 if expected=='saved' else 1)
        assert saved['cookies'][0]['value']=='1'
    finally:
        for p in workers:
            if p.poll() is None:p.kill();p.wait(timeout=5)
