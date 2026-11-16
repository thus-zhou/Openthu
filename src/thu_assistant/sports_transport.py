"""Same-origin sports transport. The school's frontend signs in its own browser."""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
import threading
import time
from urllib.parse import parse_qsl, urlsplit
from .bridge import CampusError
from .gateway_client import _control
from .catalog import state_root

ORIGIN = 'https://www.sports.tsinghua.edu.cn'
PREFIX = '/venue/site'
HOME = ORIGIN + '/venue/index.html'
PERSONAL = HOME + '#/personal?type=0'
SCENE = '/api/site/scene/list'
LOCATION = '/api/site/choose'
PAGE = '/api/reserve/current/page'
DETAIL = '/api/reserve/details'
RECORDS = '/api/reserve/reserveRecord'
CAPTCHA = '/api/reserve/enableValidCode'
USER = '/system/login/getLoginUser'
FORM_BRIEF = '/workflow/process/brief/'
CREATE = '/api/reserve/addReserve'
CANCEL = '/api/reserve/cancelReserve'
READS = {SCENE:'GET', LOCATION:'GET', PAGE:'POST', DETAIL:'GET', RECORDS:'POST', CAPTCHA:'GET', USER:'GET'}
READ_KEYS = {
    PAGE: {'sceneUuid', 'siteType', 'sceneUseType', 'classTypeUuid', 'classTypeEnum', 'reserveDate',
           'resvKind', 'searchValue', 'siteKindId', 'pageSize', 'pageNum'},
    RECORDS: {'pageSize', 'pageNum', 'containsActivity', 'orderItems', 'orderRule', 'range',
              'orderStatus', 'refundStatus', 'resvStatus', 'resvExtendStatus', 'resvCheckStatus'},
    DETAIL: {'resvUuid'}, CAPTCHA: set(),
}
SIGN = {'appId', 'nonce', 'timeStamp', 'sign'}
SERIAL = threading.RLock()


def sports_read_allowed(url, method, data):
    path = url.path[len(PREFIX):]
    if READS.get(path) != method or path not in READ_KEYS:
        return False
    query = dict(parse_qsl(url.query))
    if method == 'GET':
        return set(query) <= (READ_KEYS[path] | SIGN)
    try:
        value = json.loads(data or '')
    except (ValueError, TypeError):
        return False
    return isinstance(value, dict) and set(value) <= READ_KEYS[path] and set(query) <= SIGN


# fe31 is the client actually imported by the official PC reservation module.
# Run its request interceptor in the page. Neither token nor signing key leaves
# the browser. Use fetch directly so a response interceptor cannot replay a write.
WIRE = """async ({path,method,params,prepare}) => {
  const origin='https://www.sports.tsinghua.edu.cn';
  if(location.origin!==origin)throw Error('ORIGIN');
  if(!window.__thuSportClient) {
    let req;const id='thuSportsClient'+Math.random().toString(36).slice(2);
    window.webpackJsonp.push([[id],{[id]:(m,e,r)=>{req=r}},[[id]]]);
    window.__thuSportClient=req('fe31').a;
  }
  const cfg=await window.__thuSportClient.interceptors.request.handlers[0].fulfilled({headers:{},params:{}});
  if(!cfg.headers.token || !cfg.params.sign)throw Error('CLIENT_OR_AUTH');
  const url=new URL('/venue/site'+path,origin);
  for(const [k,v] of Object.entries({...cfg.params,...(method==='GET'?params:{})}))url.searchParams.set(k,v);
  const body=method==='POST'?JSON.stringify(params):undefined;
  const init={method,headers:{...cfg.headers,'x-api-version':'2.0.0','Content-Type':'application/json'},body,redirect:'error'};
  if(prepare) {
    window.__thuSportPending={url:url.href,init};
    return {url:url.href,body};
  }
  const ctl=new AbortController(), timer=setTimeout(()=>ctl.abort(),18000);
  try {
    const res=await fetch(url,{...init,signal:ctl.signal});
    let payload=null;try{payload=await res.json()}catch(e){}
    return {status:res.status,retry:res.headers.get('retry-after'),payload};
  } finally {clearTimeout(timer)}
}"""


def delay_seconds(header):
    if header is None:
        return None
    if str(header).isdigit():
        return int(header)
    try:
        return max(0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def checked_response(response):
    http = response['status']
    value = response.get('payload')
    msg = str(value.get('message', '')) if isinstance(value, dict) else ''
    if '维护' in msg or http == 503 and '维护' in str(value):
        raise CampusError('MAINTENANCE')
    if http == 429 or '频繁' in msg or '限流' in msg:
        delay = delay_seconds(response.get('retry'))
        raise CampusError('RATE_LIMITED', None if delay is None else int(delay * 1000))
    if http == 401 or '登录失效' in msg or '重新登录' in msg:
        raise CampusError('AUTH_REQUIRED')
    if http == 403 or '无权限' in msg:
        raise CampusError('FORBIDDEN')
    if http >= 500:
        delay = delay_seconds(response.get('retry'))
        raise CampusError('SPORTS_NETWORK_ERROR', None if delay is None else int(delay * 1000))
    if http != 200 or not isinstance(value, dict) or type(value.get('code')) is not int:
        raise CampusError('SPORTS_RESPONSE_INVALID')
    if value['code'] != 0 or value.get('success') is False:
        raise CampusError('SPORTS_QUERY_REJECTED')
    return value


def wait_or_cancel(seconds):
    event, _ = _control.get()
    if event:
        if event.wait(seconds):
            raise CampusError('CANCELLED')
    else:
        time.sleep(seconds)


@contextmanager
def site_turn():
    """One sports-site reader per local user, including separate MCP processes."""
    root = state_root() / 'sports-scheduler'
    root.mkdir(parents=True,exist_ok=True)
    with open(root/'reader.lock','a+b') as lock:
        if not lock.tell():lock.write(b'0');lock.flush()
        until = time.monotonic()+45
        while True:
            lock.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic()>=until:raise CampusError('SPORTS_BUSY',1000) from None
                wait_or_cancel(.1)
        try:yield
        finally:
            lock.seek(0)
            if os.name=='nt':msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(lock,fcntl.LOCK_UN)


class Reader:
    def __init__(self, browsers):
        self.browsers = browsers
        self.sid = None

    def __enter__(self):
        self.stack = ExitStack()
        try:
            self.stack.enter_context(SERIAL)
            self.stack.enter_context(site_turn())
            session = self.browsers.open(service_id='sports', visible=False)
            self.sid = session['session_id']
            if session.get('status') == 'TWO_FACTOR_REQUIRED':
                raise CampusError('TWO_FACTOR_REQUIRED')
            self.session = self.browsers.sessions[self.sid]
            self.page = self.session['page']
            if urlsplit(self.page.url).hostname != 'www.sports.tsinghua.edu.cn':
                raise CampusError('AUTH_REQUIRED')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self.sid is not None:
                self.browsers.close(self.sid)
                self.sid = None
        finally:
            self.stack.close()

    def call(self, path, params=None):
        method = READS.get(path)
        if re.fullmatch(re.escape(FORM_BRIEF)+r'[A-Za-z0-9_-]{1,100}',path):method='GET'
        if method is None:
            raise CampusError('UNREGISTERED_SPORTS_READ')
        for attempt in range(4):
            wait_or_cancel(0.25)
            try:
                response = self.page.evaluate(WIRE, {'path':path, 'method':method, 'params':params or {}, 'prepare':False})
            except Exception as exc:
                if 'CLIENT_OR_AUTH' in str(exc):
                    raise CampusError('AUTH_REQUIRED') from None
                failure = CampusError('SPORTS_NETWORK_ERROR')
            else:
                try:
                    return checked_response(response)
                except CampusError as exc:
                    failure = exc
            if failure.code not in ('RATE_LIMITED', 'SPORTS_NETWORK_ERROR') or attempt == 3:
                raise failure
            seconds = failure.retry_after_ms / 1000 if failure.retry_after_ms is not None else 2 ** (attempt + 1)
            if seconds > 30:
                raise failure  # return the platform delay instead of retrying early
            wait_or_cancel(seconds)

