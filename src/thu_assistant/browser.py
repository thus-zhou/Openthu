from __future__ import annotations
from datetime import datetime, timezone
import base64
import hashlib
import json
import re
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote
from .bridge import CampusError, gateway
from .catalog import request_arguments

WRITE = re.compile(r'提交|确认预约|立即预约|预订|支付|缴费|续借|取消预约|保存|确认申请|删除|submit|confirm|reserve|checkout|payment|delete', re.I)
SECRET = re.compile(r'password|passwd|csrf|xsrf|token|cookie|session|ticket|otp|vericode|i_pass|i_user|username|student.?id|学号|nonce|signature|samlrequest|samlresponse|relaystate|(?:^|[_-])sign(?:$|[_-])', re.I)
MUTATION_PATH = re.compile(r'^(?:logout|logoff|delete\w*|del\w*|remove\w*|save\w*|insert\w*|update\w*|submit\w*|cancel\w*|confirm\w*|pay\w*|renew\w*|reserve|book|apply|addFavorite|setProfile)$', re.I)

def validate_url(url):
    try:
        u = urlsplit(url)
        if u.scheme != 'https' or u.port not in (None,443) or u.username or u.password or not (u.hostname == 'tsinghua.edu.cn' or (u.hostname or '').endswith('.tsinghua.edu.cn')):
            raise ValueError()
    except ValueError:
        raise CampusError('NON_TSINGHUA_HTTPS_URL') from None
    return u

def safe_url(url):
    try:
        u = urlsplit(url)
        if u.hostname=='cab.lib.tsinghua.edu.cn' and u.path=='/ic-web/account/getMembers':
            return urlunsplit((u.scheme,u.netloc,u.path,'',''))
        return urlunsplit((u.scheme,u.netloc,re.sub(r';jsessionid=[^/]*','',u.path,flags=re.I),
            urlencode([(k,v) for k,v in parse_qsl(u.query) if not SECRET.search(k)]),''))
    except ValueError:
        return ''

def read_request(url, method='GET', data=None):
    u = urlsplit(url)
    if (u.hostname in ('seat.lib.tsinghua.edu.cn','webvpn.tsinghua.edu.cn') and
            re.search(r'/api\.php/(?:spaces/[0-9]+/book|profile/books/[0-9]+)/?$',u.path)):
        return False
    if u.hostname=='cab.lib.tsinghua.edu.cn' and u.path in ('/ic-web/reserve','/ic-web/reserve/delete') and method!='GET':
        return False
    if u.hostname == 'www.sports.tsinghua.edu.cn' and u.path.startswith('/venue/site/api/reserve/'):
        from .sports_transport import sports_read_allowed
        return sports_read_allowed(u, method, data)
    if method=='GET' and u.hostname=='cab.lib.tsinghua.edu.cn' and u.path=='/ic-web/reserve':
        keys={k for k,_ in parse_qsl(u.query)}
        availability={'sysKind','resvDates','kindIds','page','pageSize','devName','labId','campusId'}
        return keys<=availability and {'sysKind','resvDates','kindIds'}<=keys
    if method=='GET' and u.hostname=='cab.lib.tsinghua.edu.cn' and u.path=='/ic-web/reserve/resvInfo':
        keys={k for k,_ in parse_qsl(u.query)}
        own={'beginDate','endDate','needStatus','page','pageNum','orderKey','orderModel'}
        return keys<=own and {'beginDate','endDate','needStatus','page','pageNum'}<=keys
    seat_book=re.fullmatch(r'/user/index/book(?:/p/[1-9][0-9]{0,5})?',u.path) is not None and u.hostname=='seat.lib.tsinghua.edu.cn'
    seat_book=seat_book or (u.hostname=='webvpn.tsinghua.edu.cn' and re.fullmatch(r'/https/77726476706e69737468656265737421e3f24088693c6152301c9aa596522b204c02212b859d0a19/user/index/book(?:/p/[1-9][0-9]{0,5})?',u.path) is not None)
    if method=='GET' and seat_book:
        return not u.query or set(k for k,_ in parse_qsl(u.query))<= {'page','p'}
    if method=='GET' and u.hostname=='cab.lib.tsinghua.edu.cn' and u.path=='/ic-web/reserve/punishInfo':
        return True
    if method=='GET' and u.hostname=='thos.tsinghua.edu.cn' and u.path=='/fp/code/apply':
        return True  # Captcha image creation, not application submission.
    if method in ('GET','HEAD') and re.search(r'\.(?:js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|map)$',u.path,re.I):
        return True
    if any(MUTATION_PATH.fullmatch(p) for p in re.split(r'[/;.!]',unquote(u.path))):
        return False
    if any(k.lower() in ('action','method','op','operation','do') and MUTATION_PATH.fullmatch(v) for k,v in parse_qsl(u.query)):
        return False
    if method in ('GET','HEAD','OPTIONS'):
        return True
    if method != 'POST':
        return False
    if u.hostname=='idp.tsinghua.edu.cn' and u.path in ('/idp/profile/SAML2/POST/SSO','/idp/profile/SAML2/Redirect/SSO'):
        return True
    if u.hostname and u.hostname.endswith('.tsinghua.edu.cn') and u.path=='/Shibboleth.sso/SAML2/POST':
        return True
    # Known official authentication is a distinct operation from business writes.
    if u.hostname == 'id.tsinghua.edu.cn' and u.path in ('/do/off/ui/auth/login/check','/b/doubleAuth/login','/b/doubleAuth/personal/saveFinger'):
        return True
    # Read/auth endpoints observed in the corresponding official frontends.
    if (u.hostname,u.path) in {
        ('thos.tsinghua.edu.cn','/fp/fp/formHome/AllSvsByConditionpage'),
        ('thos.tsinghua.edu.cn','/fp/fp/formHome/allNum'),
        ('thos.tsinghua.edu.cn','/fp/fp/serveapply/checkService'),
        ('thos.tsinghua.edu.cn','/fp/fp/serveapply/serveInfo'),
        ('discover.lib.tsinghua.edu.cn','/tsinghuasearch/primo/jwt'),
        ('discover.lib.tsinghua.edu.cn','/tsinghuasearch/primo/translations'),
        ('discover.lib.tsinghua.edu.cn','/samlsp/saml/SSO'),
        ('discover.lib.tsinghua.edu.cn','/tsinghuasearch/primo/loans'),
        ('discover.lib.tsinghua.edu.cn','/tsinghuasearch/thirdPart/checkBizParam'),
        ('www.sports.tsinghua.edu.cn','/venue/site/cas/token'),
        ('www.sports.tsinghua.edu.cn','/venue/site/system/captcha/drag/get'),
    }:
        return True
    if data and re.search(r'"(?:action|operation|method)"\s*:\s*"(?:save|submit|delete|cancel|pay|create|update)',data,re.I):
        return False
    return bool(re.search(r'/(?:get[A-Z_][\w]*|query[\w]*|search[\w]*|list[\w]*|pageList|detail)(?:[/.?]|$)',u.path,re.I))

class BrowserManager:
    def __init__(self,actions=None):
        from .actions import ActionEngine
        self.actions=actions or ActionEngine()
        self.runtime = None
        self.browser = None
        self.sessions = {}

    def open(self, service_id=None, url=None, visible=True, via_webvpn=False):
        bootstrap = gateway('browser_state', **request_arguments(service_id,url,via_webvpn))
        from learn_assistant.auth import _dpapi_unprotect
        plain = _dpapi_unprotect(base64.b64decode(bootstrap['sealedState']))
        if plain is None:
            raise CampusError('BROWSER_SESSION_DECRYPT_FAILED')
        state = json.loads(plain)
        validate_url(state['url'])
        if self.runtime is None:
            from playwright.sync_api import sync_playwright
            self.runtime = sync_playwright().start()
        # Separate browsers permit a visible user session and a headless acceptance test.
        try:browser = self.runtime.chromium.launch(headless=not visible)
        except Exception:
            try:browser = self.runtime.chromium.launch(channel='msedge',headless=not visible)
            except Exception:raise CampusError('BROWSER_RUNTIME_UNAVAILABLE') from None
        context = browser.new_context(storage_state={'cookies':state['cookies'],'origins':state.get('origins',[])})
        page = context.new_page()
        sid = uuid.uuid4().hex
        session = {'browser':browser,'context':context,'page':page,'revision':0,'refs':{},'blocked':[],'responses':[],
                   'service_origin':state['serviceOrigin'],'sync_token':bootstrap['sealedState'],
                   'account_id':state['account'],
                   'secrets':[c['value'] for c in state['cookies'] if len(c['value'])>=6]+state.get('redactions',[]), 'request_gate':None}
        self.sessions[sid] = session
        context.on('page',lambda new_page:session.update(page=new_page))
        def observe(response):
            try:
                if response.request.resource_type not in ('xhr','fetch'):return
                validate_url(response.url)
                record={'url':safe_url(response.url),'httpStatus':response.status}
                if 'json' in response.headers.get('content-type',''):
                    data=response.json()
                    if isinstance(data,dict):
                        # Generic JSON codes are transport observations, not a
                        # substitute for a service-specific business adapter.
                        record['businessSuccess']=None
                        record['businessStatusUnverified']=True
                        body=data.get('data',data.get('result'))
                        if isinstance(body,list):record['itemCount']=len(body)
                        elif isinstance(body,dict):
                            for key in ('list','items','records','rows'):
                                if isinstance(body.get(key),list):record['itemCount']=len(body[key]);break
                session['responses'].append(record)
                session['responses']=session['responses'][-40:]
            except Exception:pass
        context.on('response',observe)
        def guard(route):
            request = route.request
            try:
                if request.is_navigation_request():
                    validate_url(request.url)
                if not self.request_allowed(session,request):
                    session['blocked'].append({'method':request.method,'url':safe_url(request.url)})
                    return route.abort()
                return route.continue_()
            except Exception:
                return route.abort()
        context.route('**/*',guard)
        try:
            # Preserve ticket-bearing URLs only in memory for the official
            # callback; redact URLs when presenting snapshots, never in transit.
            page.goto(state['url'],wait_until='domcontentloaded',timeout=45000)
        except Exception:
            # Some JS applications continue navigation after DOMContentLoaded.
            if page.url == 'about:blank':
                self.close(sid)
                raise CampusError('BROWSER_NAVIGATION_FAILED') from None
        try:self._complete_sso(session)
        except CampusError as error:
            if error.code=='TWO_FACTOR_REQUIRED':
                result=self.snapshot(sid);result['status']='TWO_FACTOR_REQUIRED';return result
            self.close(sid);raise
        return self.snapshot(sid)

    @staticmethod
    def request_allowed(session,request):
        gate=session.get('request_gate')
        if gate:
            frame=request.frame
            key=str(session['page'].frames.index(frame))+':'+frame.url
            if gate.consume(request.url,request.method,request.post_data,key):return True
            # During dispatch no heuristic POST permission can expand the contract.
            if request.method not in ('GET','HEAD','OPTIONS'):return False
            if any(s.url==request.url for s in gate.specs):return False
        return read_request(request.url,request.method,request.post_data)

    def _complete_sso(self,session):
        page=session['page'];context=session['context']
        from learn_assistant.auth import _dpapi_unprotect
        for attempt in range(3):
            page=session['page']
            self._settle(page)
            if urlsplit(page.url).hostname != 'id.tsinghua.edu.cn':
                sso=page.get_by_role('button',name=re.compile(r'^(统一认证|统一身份认证|校园统一认证|校园账号登录)$'))
                if sso.count()==1 and sso.is_visible():
                    sso.click(timeout=15000)
                    continue
                return
            # A SPA may initiate SSO only after its scripts run. Complete that
            # exact official form with the protocol bridge, not a password tool argument.
            try:
                if not self._persist_context(session):raise CampusError(session.get('persistence_error','BROWSER_STATE_NOT_SAVED'))
                extra=gateway('browser_state',url=page.url,service_origin=session['service_origin'])
                refreshed=json.loads(_dpapi_unprotect(base64.b64decode(extra['sealedState'])))
                session['sync_token']=extra['sealedState']
                context.add_cookies(refreshed['cookies'])
                session['secrets']+=refreshed.get('redactions',[])
                page.goto(refreshed['url'],wait_until='domcontentloaded',timeout=45000)
            except CampusError:raise
            except Exception:raise CampusError('BROWSER_SSO_NOT_VERIFIED') from None
        self._settle(page)

    @staticmethod
    def _settle(page):
        try:page.wait_for_load_state('networkidle',timeout=8000)
        except Exception:pass

    def _session(self,sid):
        if sid not in self.sessions:
            raise CampusError('BROWSER_SESSION_NOT_FOUND')
        return self.sessions[sid]

    def _redact(self,value,session):
        text = str(value)
        for secret in sorted(session['secrets'],key=len,reverse=True):
            text = text.replace(secret,'[REDACTED]')
        return re.sub(r'([?&](?:ticket|_csrf|token|oauth_token)=)[^\s&"\'<>]*',r'\1[REDACTED]',text,flags=re.I)

    @staticmethod
    def _persist_context(s):
        try:
            from learn_assistant.auth import _dpapi_protect
            stored=s['context'].storage_state()
            cookies=[c for c in stored['cookies'] if (c.get('domain','').lstrip('.')=='tsinghua.edu.cn' or c.get('domain','').endswith('.tsinghua.edu.cn'))]
            origins=[]
            for origin in stored['origins']:
                try:validate_url(origin['origin']);origins.append(origin)
                except CampusError:continue
            protected=_dpapi_protect(json.dumps({'cookies':cookies,'origins':origins,'syncToken':s.get('sync_token')},ensure_ascii=False).encode())
            if protected:
                result=gateway('import_browser_state',sealedState=base64.b64encode(protected).decode())
                s['sync_token']=result['syncToken']
                s.pop('persistence_error',None)
                return True
            s['persistence_error']='STATE_ENCRYPT_FAILED'
        except CampusError as error:s['persistence_error']=error.code
        except Exception:s['persistence_error']='BROWSER_STATE_SYNC_FAILED'
        return False

    def snapshot(self,session_id,offset=0,limit=24000):
        s = self._session(session_id)
        s['secrets'] += [c['value'] for c in s['context'].cookies() if len(c.get('value',''))>=6]
        for origin in s['context'].storage_state().get('origins',[]):
            s['secrets'] += [v['value'] for v in origin.get('localStorage',[]) if SECRET.search(v.get('name','')) and len(v.get('value',''))>=6]
        s['secrets'] = list(set(s['secrets']))
        s['revision'] += 1
        s['refs'] = {}
        elements = []
        texts = []
        for fi,frame in enumerate(s['page'].frames):
            try:
                validate_url(frame.url)
                texts.append(frame.locator('body').inner_text(timeout=5000))
                # Campus SPAs often render clickable cards as div/span rather
                # than accessible buttons. Include visible pointer controls.
                frame.evaluate('''() => {
                  document.querySelectorAll('[data-thu-control]').forEach(e=>e.removeAttribute('data-thu-control'));
                  let n=0;
                  for(const e of document.querySelectorAll('a,button,input:not([type=hidden]),textarea,select,[role=button],[onclick],[tabindex],label,summary,div,span,li')) {
                    if(n>=400)break;
                    const box=e.getBoundingClientRect();if(!box.width||!box.height)continue;
                    const native=e.matches('a,button,input,textarea,select,[role=button],[onclick],[tabindex],label,summary');
                    const pointer=e.children.length<6&&getComputedStyle(e).cursor==='pointer'&&(e.innerText||e.getAttribute('aria-label'));
                    if(native||pointer)e.setAttribute('data-thu-control',String(n++));
                  }
                }''')
                candidates = frame.locator('[data-thu-control]')
                for i in range(min(candidates.count(),400)):
                    element = candidates.nth(i)
                    if not element.is_visible():
                        continue
                    meta = element.evaluate('''e => ({tag:e.tagName.toLowerCase(),type:e.type||'',name:e.name||'',
                      label:e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.name||'',
                      href:e.tagName==='A'?e.href:'',value:e.value||''})''')
                    ref = f'f{fi}-e{i}'
                    s['refs'][ref] = element
                    secret = meta['type']=='password' or SECRET.search(meta['name'])
                    elements.append({'id':ref,'tag':meta['tag'],'type':meta['type'],
                        'label':self._redact(meta['label'],s)[:120], 'value':'[REDACTED]' if secret else self._redact(meta['value'],s)[:180],
                        'url':self._redact(safe_url(meta['href']),s) if meta['href'] else '',
                        'requiresActionPlan':bool(meta['type']=='submit' or WRITE.search(meta['label']))})
            except Exception:
                continue
        persisted=self._persist_context(s)
        current_url=s['page'].url
        text=self._redact('\n'.join(texts),s)
        offset=max(0,int(offset));limit=min(100000,max(1,int(limit)))
        status='BROWSER_PAGE_OBSERVED_NOT_TRANSACTION_VERIFIED'
        if current_url.startswith('chrome-error:'):status='BROWSER_NAVIGATION_ERROR'
        elif urlsplit(current_url).hostname in ('id.tsinghua.edu.cn','idp.tsinghua.edu.cn'):status='AUTHENTICATION_PAGE'
        elif re.search(r'拖动.*滑块|滑动.*验证|请完成.*验证|请完成.*拼图|安全验证', '\n'.join(texts)):status='HUMAN_VERIFICATION_MAY_BE_REQUIRED'
        return {'session_id':session_id,'snapshot_id':s['revision'],'url':self._redact(safe_url(s['page'].url),s),
            'title':self._redact(s['page'].title(),s),'text':text[offset:offset+limit],
            'totalCharacters':len(text),'offset':offset,'nextOffset':offset+limit if offset+limit<len(text) else None,
            'elements':elements,'blockedRequests':s['blocked'][-10:],'networkObservations':s['responses'][-20:],'sessionPersisted':persisted,
            'sessionPersistenceError':s.get('persistence_error'),
            'source':{'url':safe_url(s['page'].url),'kind':'visible_browser_frames'},
            'checked_at':datetime.now(timezone.utc).isoformat(),
            'complete':False,'snapshot_complete':offset+limit>=len(text),'next_cursor':None,
            'warnings':['DYNAMIC_CONTENT_COMPLETENESS_UNKNOWN'],'verification_level':'PAGE_OBSERVATION',
            'status':status}

    def scroll(self,session_id,pixels=650,frame_index=0):
        s=self._session(session_id)
        frames=s['page'].frames
        if not 0<=frame_index<len(frames):raise CampusError('FRAME_NOT_FOUND')
        validate_url(frames[frame_index].url)
        frames[frame_index].evaluate('(n)=>window.scrollBy(0,n)',pixels)
        self._settle(s['page'])
        return self.snapshot(session_id)

    def _element(self,sid,snapshot_id,element_id):
        s=self._session(sid)
        if s['revision']!=snapshot_id or element_id not in s['refs']:
            raise CampusError('STALE_SNAPSHOT_TAKE_NEW_SNAPSHOT')
        return s,s['refs'][element_id]

    def fill(self,session_id,snapshot_id,element_id,value):
        s,element=self._element(session_id,snapshot_id,element_id)
        if element.get_attribute('type')=='password' or SECRET.search(element.get_attribute('name') or ''):
            raise CampusError('USE_OFFICIAL_AUTH_FLOW_FOR_SECRETS')
        if element.get_attribute('type') in ('checkbox','radio'):
            if value.lower() in ('true','1','yes','选中'):element.check()
            else:element.uncheck()
        elif element.evaluate('e=>e.tagName.toLowerCase()')=='select':
            element.select_option(value)
        else:
            element.fill(value)
        return self.snapshot(session_id)

    def click(self,session_id,snapshot_id,element_id):
        s,element=self._element(session_id,snapshot_id,element_id)
        label=element.inner_text() or element.get_attribute('aria-label') or ''
        if element.get_attribute('type')=='submit' or WRITE.search(label):
            raise CampusError('PREPARE_CONCRETE_ACTION_BEFORE_SUBMIT')
        element.click(timeout=15000)
        self._complete_sso(s)
        return self.snapshot(session_id)

    def _form_state(self,s,element):
        state=element.evaluate('''e => {
          const f=e.form||e.closest('form'), root=f||document;
          const native=f?Array.from(f.elements):Array.from(root.querySelectorAll('input,textarea,select'));
          const custom=Array.from(root.querySelectorAll('[aria-checked],[aria-selected],[data-state],[data-selected]'));
          document.__thuActionDocumentId ||= crypto.randomUUID();
          return {document:document.__thuActionDocumentId,frameUrl:location.href,
            action:f?f.action:'',method:f?f.method:'',
            fields:Array.from(new Set([...native,...custom])).filter(x=>x.type!=='password').map(x=>({
              name:x.name||x.id||x.getAttribute('aria-label')||'',type:x.type||x.getAttribute('role')||x.tagName.toLowerCase(),
              value:x.value??x.getAttribute('data-value')??'',disabled:!!x.disabled,
              checked:['checkbox','radio'].includes(x.type)?x.checked:null,
              selected:x.tagName==='SELECT'?Array.from(x.selectedOptions).map(o=>o.value):null,
              ariaChecked:x.getAttribute('aria-checked'),ariaSelected:x.getAttribute('aria-selected'),
              dataState:x.getAttribute('data-state'),dataSelected:x.getAttribute('data-selected')
            }))};}''')
        state['fields']=[f for f in state['fields'] if not SECRET.search(f['name'])]
        state['frameUrl']=safe_url(state['frameUrl']);state['action']=safe_url(state['action'])
        frame=element.element_handle().owner_frame()
        state['frameIndex']=s['page'].frames.index(frame)
        payload={'url':safe_url(s['page'].url),'label':element.inner_text() or element.get_attribute('aria-label') or '', **state}
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return payload,digest

    def prepare(self,session_id,snapshot_id,element_id,purpose):
        s,element=self._element(session_id,snapshot_id,element_id)
        payload,_=self._form_state(s,element)
        result=self.actions.prepare_browser(payload,s['account_id'],{'session_id':session_id,'document':payload.get('document'),'frame_index':payload.get('frameIndex')})
        # Generic fields cannot be reliably classified as nonpersonal without a site adapter.
        result.update(expiresInSeconds=600,submitted=False,form_complete=False,
            fields=[{'type':f['type'],'value':'[REDACTED]','checked':f.get('checked'),
                'selected_count':len(f.get('selected') or [])} for f in payload['fields']],
            authorizationRequired='A specific human instruction plus trusted host evidence and a registered site adapter are required.')
        return result

    def execute(self,plan_id,authorization):
        return self.actions.execute(plan_id,authorization)

    def close(self,session_id):
        s=self.sessions.pop(session_id,None)
        if s:
            try:s['context'].close()
            except Exception:pass
            try:s['browser'].close()
            except Exception:pass
        return {'closed':bool(s)}

    def close_all(self):
        for sid in list(self.sessions):self.close(sid)
        if self.runtime:self.runtime.stop();self.runtime=None
