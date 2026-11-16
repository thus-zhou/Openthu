"""Fail-closed transaction framework. No campus write adapter is enabled in block 5."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib,json,os,re,time,uuid
from urllib.parse import urlsplit,parse_qsl
from .action_store import ActionStore
from .bridge import CampusError,gateway
from .queries import validate,schema,safe_url

FIELDS={'target':{'type':'string','minLength':1,'maxLength':240},'date':{'type':'string','maxLength':32},
 'start_time':{'type':'string','maxLength':32},'end_time':{'type':'string','maxLength':32},
 'members':{'type':'string','maxLength':2000},'headcount':{'type':'integer','minimum':1,'maximum':1000},
 'fee':{'type':'string','maxLength':32},'payment_method':{'type':'string','maxLength':80},'record_id':{'type':'string','maxLength':120}}
REQUIRED_CREATE=('target','date','start_time','end_time','members','headcount','fee','payment_method')
CHECKS=('availability','eligibility','fees','rules')
REGISTRY={(service,operation):{'version':'action-v1','schema':schema(FIELDS),'required':REQUIRED_CREATE if operation=='create' else ('target','record_id'),
 'preview_fields':list(FIELDS),'critical_fields':list(FIELDS),'prechecks':CHECKS,
 'risk_conditions':['ACCOUNT_CHANGED','EXPIRED','BUSINESS_OR_PAGE_CHANGED','NO_TRUSTED_HUMAN_EVIDENCE','REMOTE_RESULT_UNCERTAIN'],
 'request_sequence':'REQUIRES_SITE_ADAPTER','reconciliation':'EXACT_BUSINESS_FIELDS_AND_REMOTE_RECORD_ID','enabled':False}
 for service in ('sports','library-seat','library-rooms','visitors') for operation in ('create','cancel')}

# The legacy create/cancel contracts stay available for existing framework clients.
SPORT_ID = {'type':'string','pattern':r'[A-Za-z0-9_-]{1,100}'}
SPORT_INPUT = {'scene_id':SPORT_ID,'room_id':SPORT_ID,'site_id':SPORT_ID,'session_id':SPORT_ID,
               'date':{'type':'string','pattern':r'\d{4}-\d{2}-\d{2}'},
               'mode':{'type':'string','pattern':'SPORT_GROUP|SPORT_PERSON'},
               'members':FIELDS['members'],'payment_method':{'type':'string','pattern':'PAY_ONLINE|PAY_OFFLINE|FREE'}}
for op, props in [('create_reservation',SPORT_INPUT),('cancel_reservation',{'record_id':SPORT_ID})]:
    required = tuple(props)
    if op=='create_reservation':props={**props,'expected_total_fen':{'type':'integer','minimum':0,'maximum':10000000}}
    outputs = dict(props)
    outputs['scene_id']=SPORT_ID
    for field in ('target','start_time','end_time','fee','cancel_rule','official_url','handoff',
                  'account_member','fingerprint','site_ids','refund_state','original_status'):
        outputs[field] = {'type':'string','maxLength':4000}
    REGISTRY[('sports',op)] = {
        'version':'sports-action-v1','input_schema':schema(props),'schema':schema(outputs),'required':required,
        'preview_fields':list(outputs),'critical_fields':list(outputs),'prechecks':CHECKS,
        'risk_conditions':['PRICE_OR_AVAILABILITY_CHANGED','OFFICIAL_VERIFICATION','RESULT_UNKNOWN'],
        'request_sequence':'ONE_EXACT_REQUEST_OR_OFFICIAL_HANDOFF',
        'reconciliation':'OWN_RECORD_DETAIL_EXACT_MATCH','enabled':True}

from . import library_contracts as library_actions
REGISTRY.update(library_actions.contracts())
VERIFIED_ADAPTERS = {library_actions.VERSION, "sports-action-v1"}

def action_enabled(service):
    return library_actions.enabled() if service=="library" else sports_enabled()

def sports_enabled():
    return os.environ.get('THU_SPORTS_WRITE','1') != '0' and os.environ.get('THU_SPORTS_READ','1') != '0'

TRANSITIONS={
 'PREPARED':{'DISPATCHING','FAILED','AWAITING_OFFICIAL'},'AWAITING_OFFICIAL':{'AWAITING_OFFICIAL','UNKNOWN','SUCCEEDED','PENDING_PAYMENT','PENDING_APPROVAL','FAILED'},'NEEDS_USER_ACTION':set(),
 'DISPATCHING':{'UNKNOWN','FAILED'},'UNKNOWN':{'UNKNOWN','SUCCEEDED','FAILED','PENDING_PAYMENT','PENDING_APPROVAL'},
 'PENDING_PAYMENT':{'UNKNOWN','SUCCEEDED','FAILED','PENDING_PAYMENT','PENDING_APPROVAL'},
 'PENDING_APPROVAL':{'UNKNOWN','SUCCEEDED','FAILED','PENDING_PAYMENT','PENDING_APPROVAL'},
 'SUCCEEDED':set(),'FAILED':set()}

def describe_actions(service_id):
    return [{'operation':op,'parameters_schema':deepcopy(c.get('input_schema',c['schema'])),'required_business_fields':list(c['required']),
        'adapter_version':c['version'],'enabled':c['enabled'] and action_enabled(service_id),'reason':('READY' if action_enabled(service_id) else 'WRITES_DISABLED') if c['enabled'] else 'ADAPTER_REQUIRED',
        'request_sequence':c['request_sequence'],'prechecks':list(c['prechecks']),
        'critical_fields':c['critical_fields'],'risk_conditions':c['risk_conditions'],'reconciliation':c['reconciliation']}
        for (service,op),c in REGISTRY.items() if service==service_id]

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def identity():return gateway('action_identity')['account_id']
def process_alive(pid):
    if not isinstance(pid,int) or pid<=0:return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];api.OpenProcess.restype=wintypes.HANDLE
        api.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=api.OpenProcess(0x1000,False,pid)
        if not handle:return ctypes.get_last_error()==5 # Access denied means unknown, not dead.
        try:
            status=wintypes.DWORD()
            return not api.GetExitCodeProcess(handle,ctypes.byref(status)) or status.value==259
        finally:api.CloseHandle(handle)
    try:os.kill(pid,0);return True
    except PermissionError:return True
    except ProcessLookupError:return False

def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate JSON key')
        result[key]=value
    return result
def mask(value):return '[REDACTED]' if value else value
def public(record):
    fields={k:(mask(v) if k not in {'target','date','start_time','end_time','headcount','fee','payment_method'} else v) for k,v in record['business'].items()}
    if record.get('adapter_version')=='sports-action-v1':
        for key in ('scene_id','room_id','site_id','session_id','mode','record_id','cancel_rule','official_url','handoff','refund_state','original_status'):
            if key in record['business']:fields[key]=record['business'][key]
        if 'members' in record['business']:fields['members']='本人' if record['business']['members']=='self' else '按指定参加者在官方页核对'
    if record.get('adapter_version')==library_actions.VERSION:
        fields={k:v for k,v in record['business'].items() if k in library_actions.PUBLIC}
        if 'members' in record['business']:fields['member_count']=len(record['business']['members'])
    extra={}
    if record.get('adapter_version')=='sports-action-v1':
        if 'expected_total_fen' in record['business']:fields['expected_total_fen']=record['business']['expected_total_fen']
        extra.update(official_url=record['business'].get('official_url'),platform=record.get('platform'),checked_at=record.get('checked_at'))
    if record.get('adapter_version')==library_actions.VERSION:
        extra.update(official_url=record['business'].get('official_url'),platform=record.get('platform'),checked_at=record.get('checked_at'))
        if record.get('platform'):
            extra['todo_query']={'services':['library.'+record['business']['system']],
                                 'start_date':record['business']['date'],'end_date':record['business']['date'],'include_completed':True}
    return {**extra,'operation_id':record['operation_id'],'plan_id':record['operation_id'],'status':record['state'],
        'service_id':record['service_id'],'operation':record['operation'],'preview':fields,'expires_at':record['expires_at'],
        'missing_fields':record.get('missing_fields',[]),'reason':record.get('reason'),'adapter_version':record['adapter_version'],
        'verification_level':'REMOTE_RECORD_VERIFIED' if record['state'] in {'SUCCEEDED','PENDING_PAYMENT','PENDING_APPROVAL'} else 'LOCAL_EXECUTION_STATE',
        'record_reference':mask(record.get('record_id')),'authorization_recorded':bool(record.get('authorization')),
        'authorization_is_not_consent_proof':True,'automatic_resend':False,'revision':record['revision']}

@dataclass(frozen=True)
class RequestSpec:
    url:str
    method:str
    body:str|None=None
    frame:str=''
    encoding:str='json'

def canonical_body(value,encoding):
    if value is None:return None
    if encoding=='json':return json.dumps(json.loads(value,object_pairs_hook=unique_object),sort_keys=True,separators=(',',':'),ensure_ascii=False)
    if encoding=='form':return sorted(parse_qsl(value,keep_blank_values=True))
    if encoding=='raw':return value
    raise CampusError('REQUEST_ENCODING_NOT_REGISTERED')

class RequestGate:
    """Exact, ordered, single-use request capabilities; no origin/time-window grant."""
    def __init__(self,specs):
        from .browser import validate_url
        if not specs or len(specs)>10:raise CampusError('REQUEST_SEQUENCE_REQUIRED')
        self.specs=tuple(specs);self.sent=0;self.closed=False
        for spec in self.specs:
            validate_url(spec.url)
            if spec.method not in {'POST','PUT','PATCH','DELETE','GET'}:raise CampusError('REQUEST_METHOD_INVALID')
            canonical_body(spec.body,spec.encoding)
    def consume(self,url,method,body=None,frame=''):
        if self.closed or self.sent>=len(self.specs):return False
        expected=self.specs[self.sent]
        try:match=(url==expected.url and method==expected.method and frame==expected.frame and canonical_body(body,expected.encoding)==canonical_body(expected.body,expected.encoding))
        except Exception:return False
        if match:self.sent+=1
        return match
    def close(self):self.closed=True

class NotDispatched(Exception):
    """A trusted adapter has proof that no request was sent."""

class ActionEngine:
    def __init__(self,store=None,account_provider=identity,adapters=None,consent_verifier=None,clock=time.time):
        self.store=store or ActionStore();self.account_provider=account_provider;self.adapters=adapters or {}
        # This callback comes only from a trusted host integration, never MCP parameters,
        # website data or an authorization string. No host integration ships in block 5.
        self.consent_verifier=consent_verifier or (lambda record:None);self.clock=clock
    def _bound(self,record):
        if self.account_provider()!=record['account']:raise CampusError('ACTION_ACCOUNT_MISMATCH')
    def _adapter(self,record):
        adapter=self.adapters.get((record['service_id'],record['operation']))
        if adapter and adapter.version!=record['adapter_version']:raise CampusError('ACTION_ADAPTER_CHANGED')
        return adapter
    def prepare(self,service_id,operation,parameters):
        contract=REGISTRY.get((service_id,operation))
        if not contract:raise CampusError('UNREGISTERED_ACTION_OPERATION')
        validate(parameters,contract.get('input_schema',contract['schema']));business=deepcopy(parameters)
        if contract['enabled'] and not action_enabled(service_id):raise CampusError('LIBRARY_WRITES_DISABLED' if service_id=='library' else 'SPORTS_WRITES_DISABLED')
        account=self.account_provider()
        missing=[k for k in contract['required'] if k not in business or business[k]=='']
        adapter=self.adapters.get((service_id,operation))
        if adapter and not missing:
            business=adapter.preview(deepcopy(business));validate(business,contract['schema'])
            missing=[k for k in contract['required'] if k not in business or business[k]=='']
        return self._prepare(service_id,operation,business,missing,adapter.version if adapter else contract['version'],None,account)
    def _prepare(self,service,operation,business,missing,version,context,expected_account):
        account=self.account_provider()
        if not account or account!=expected_account:raise CampusError('ACTION_ACCOUNT_MISMATCH')
        record={'version':1,'operation_id':uuid.uuid4().hex,'service_id':service,'operation':operation,
            'account':account,'business':deepcopy(business),'digest':digest(business),'context':context,
            'adapter_version':version,'expires_at':self.clock()+600,'state':'NEEDS_USER_ACTION' if missing else 'PREPARED',
            'missing_fields':missing,'reason':'MISSING_BUSINESS_FIELDS' if missing else None,'revision':0,'events':[]}
        if not self.adapters.get((service,operation)):record['reason']='ADAPTER_REQUIRED' if not missing else 'MISSING_BUSINESS_FIELDS'
        self.event(record,record['state'],record['reason']);return public(self.store.create(record))
    def prepare_browser(self,payload,account,context):
        if account!=self.account_provider():raise CampusError('ACTION_ACCOUNT_MISMATCH')
        # Generic form fields stay encrypted; the generic form has no write contract.
        return self._prepare('browser','submit',payload,[], 'browser-preview-v1',context,account)
    def event(self,record,state,reason=None):
        if record['events'] and state not in TRANSITIONS[record['state']]:raise CampusError('INVALID_ACTION_TRANSITION')
        record.update(state=state,reason=reason)
        record['events'].append({'state':state,'reason':reason,'at':self.clock()})
    def execute(self,plan_id,authorization):
        record=self.store.read(plan_id);self._bound(record)
        if record['state'] not in {'PREPARED','NEEDS_USER_ACTION'}:return self.result(plan_id)
        if record['state']=='NEEDS_USER_ACTION':return public(record)
        def invalidate(reason):
            def update(r):
                if r['state']!='PREPARED' or r['revision']!=record['revision']:raise CampusError('ACTION_ALREADY_CLAIMED')
                self.event(r,'FAILED',reason)
            return public(self.store.update(plan_id,update))
        if self.clock()>=record['expires_at']:
            return invalidate('ACTION_PLAN_EXPIRED')
        adapter=self._adapter(record)
        if not adapter:raise CampusError('ADAPTER_REQUIRED')
        if record['adapter_version']=='sports-action-v1' and not sports_enabled():raise CampusError('SPORTS_WRITES_DISABLED')
        if record['adapter_version']==library_actions.VERSION and not library_actions.enabled():raise CampusError('LIBRARY_WRITES_DISABLED')
        if not isinstance(authorization,str) or len(authorization.strip())<8:raise CampusError('SPECIFIC_HUMAN_AUTHORIZATION_REQUIRED')
        grant=self.consent_verifier(deepcopy(record))
        expected={'plan_id':plan_id,'account':record['account'],'digest':record['digest'],'adapter_version':record['adapter_version']}
        if not isinstance(grant,dict) or any(grant.get(k)!=v for k,v in expected.items()) or grant.get('source')!='human' or not grant.get('reference') or grant.get('expires_at',0)<=self.clock():
            raise CampusError('TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED')
        try:
            fresh=adapter.preflight(deepcopy(record))
            handoff=fresh.get('official_handoff') if record['adapter_version'] in VERIFIED_ADAPTERS else None
            if digest(fresh['business'])!=record['digest'] or fresh.get('context')!=record['context'] or (not handoff and any(fresh.get('checks',{}).get(k) is not True for k in CHECKS)):
                return invalidate('PRECONDITIONS_CHANGED_PREPARE_AGAIN')
            gate=None if handoff else RequestGate(fresh['requests'])
        except CampusError:raise
        except Exception:raise CampusError('ACTION_PREFLIGHT_FAILED') from None
        self._bound(record)
        if self.clock()>=record['expires_at'] or self.clock()>=grant['expires_at']:raise CampusError('ACTION_PLAN_EXPIRED')
        def claim(r):
            if r['state']!='PREPARED' or r['revision']!=record['revision']:raise CampusError('ACTION_ALREADY_CLAIMED')
            if self.clock()>=r['expires_at'] or self.clock()>=grant['expires_at']:raise CampusError('ACTION_PLAN_EXPIRED')
            r['authorization']={'record':authorization,'evidence':grant};r['owner_pid']=os.getpid();r['dispatch_at']=self.clock()
            if handoff:self.event(r,'AWAITING_OFFICIAL',handoff)
            else:self.event(r,'DISPATCHING')
        try:record=self.store.update(plan_id,claim)
        except CampusError as e:
            if e.code=='ACTION_ALREADY_CLAIMED':return self.result(plan_id)
            raise
        if handoff:return public(record)
        # DISPATCHING is durable before the adapter receives the one-use request gate.
        try:
            receipt=adapter.dispatch(deepcopy(record),gate)
            if record['adapter_version']==library_actions.VERSION and gate.sent==1 and receipt is True:
                self.store.update(plan_id,lambda r:r.update(platform_accepted=True))
        except NotDispatched:
            if gate.sent==0:return public(self.store.update(plan_id,lambda r:self.event(r,'FAILED','REQUEST_NOT_SENT')))
        except Exception:
            pass # A missing response never authorizes an automatic replay.
        finally:gate.close()
        def uncertain(r):
            if r['state']=='DISPATCHING':self.event(r,'UNKNOWN','RECONCILIATION_REQUIRED')
        record=self.store.update(plan_id,uncertain)
        if record['state'] not in {'UNKNOWN','PENDING_PAYMENT','PENDING_APPROVAL'}:return public(record)
        return self._reconcile(record,adapter)
    def _reconcile(self,record,adapter):
        revision=record['revision'];oid=record['operation_id']
        try:
            evidence=adapter.reconcile(deepcopy(record))
            rows=evidence.get('records');complete=evidence.get('complete') is True
            matched=[r for r in (rows or []) if r.get('business')==record['business'] and r.get('operation')==record['operation'] and r.get('account')==record['account'] and r.get('service_id')==record['service_id']]
            if complete and len(matched)==1 and matched[0].get('record_id') and matched[0].get('state') in {'SUCCEEDED','PENDING_PAYMENT','PENDING_APPROVAL','FAILED'}:
                state=matched[0]['state'];reason='REMOTE_RECORD_MATCHED';remote=matched[0]['record_id']
            else:state='AWAITING_OFFICIAL' if record['state']=='AWAITING_OFFICIAL' and complete and not matched else 'UNKNOWN';reason='DUPLICATE_REMOTE_RECORDS' if len(matched)>1 else 'REMOTE_RESULT_NOT_PROVEN';remote=None
        except Exception:state='UNKNOWN';reason='RECONCILIATION_UNAVAILABLE';remote=None
        self._bound(record)
        def update(r):
            if r['revision']!=revision:raise CampusError('ACTION_RESULT_CHANGED_RECHECK')
            self.event(r,state,reason)
            if remote:r['record_id']=remote
            if r['adapter_version'] in VERIFIED_ADAPTERS:
                from datetime import datetime, timezone
                r['checked_at']=datetime.now(timezone.utc).isoformat()
                r['platform']=matched[0].get('platform') if remote else None
        return public(self.store.update(oid,update))
    def result(self,operation_id):
        record=self.store.read(operation_id);self._bound(record)
        if record['state']=='DISPATCHING':
            alive=process_alive(record.get('owner_pid'))
            if alive and self.clock()-record['dispatch_at']<120:return public(record)
            def interrupted(r):
                if r['state']=='DISPATCHING' and r['revision']==record['revision']:self.event(r,'UNKNOWN','DISPATCH_INTERRUPTED_RECONCILE_ONLY')
            record=self.store.update(operation_id,interrupted)
        if record['state'] in {'UNKNOWN','PENDING_PAYMENT','PENDING_APPROVAL','AWAITING_OFFICIAL'}:
            adapter=self._adapter(record)
            if adapter:return self._reconcile(record,adapter)
        return public(record)
