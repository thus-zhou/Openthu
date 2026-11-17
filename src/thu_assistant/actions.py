"""Fail-closed transaction framework. No campus write adapter is enabled in block 5."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib,json,os,re,time,uuid
from urllib.parse import urlsplit,parse_qsl
from .action_store import ActionStore
from .bridge import CampusError,gateway
from .consent import DECLINED, NOT_APPLICABLE, VERSION as HOST_AUTHORIZATION_VERSION, HostConsent, new_challenge
from .queries import validate,schema,safe_url

# A trusted in-process confirmation that returns None means "declined" on
# Windows, where the only production callback is the native message box.  On
# other platforms None means "this confirmation path does not apply", so a
# pending signed grant may still be used.
NATIVE_CONFIRMATION_DECLINES_ON_NONE = os.name == 'nt'

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

# Entering conditions for every persisted state.  The action engine enforces a
# subset mechanically; this table is the shared contract for adapters, the
# facade and recovery, and must change with the transition map.
STATE_ENTRY={
 'PREPARED':{'entered_by':'prepare with every required business field; reason ADAPTER_REQUIRED marks a plan that cannot execute until an adapter is registered','authorization':'REQUIRED_NOT_CONSUMED','remote_write':'NONE'},
 'NEEDS_USER_ACTION':{'entered_by':'prepare is missing required business fields','authorization':'NOT_APPLICABLE','remote_write':'NONE'},
 'AWAITING_OFFICIAL':{'entered_by':'preflight handed the step to the official site; no request was sent','authorization':'RECORDED_FOR_HANDOFF_ONLY','remote_write':'NONE'},
 'DISPATCHING':{'entered_by':'the atomic claim saved authorization, dispatch intent and owner process','authorization':'CONSUMED','remote_write':'MAY_HAVE_STARTED'},
 'UNKNOWN':{'entered_by':'dispatch returned without proof, or reconciliation could not match exactly one record','authorization':'CONSUMED','remote_write':'UNKNOWN'},
 'PENDING_APPROVAL':{'entered_by':'exactly one matching remote record reports approval pending','authorization':'CONSUMED','remote_write':'ACCEPTED'},
 'PENDING_PAYMENT':{'entered_by':'exactly one matching remote record reports payment pending','authorization':'CONSUMED','remote_write':'ACCEPTED'},
 'SUCCEEDED':{'entered_by':'exactly one matching remote record is a terminal success','authorization':'CONSUMED','remote_write':'ACCEPTED'},
 'FAILED':{'entered_by':'plan expiry, a definite not-sent error, changed preconditions or one matching remote failure','authorization':'NOT_CONSUMABLE','remote_write':'NONE_OR_FAILED'},
}

EFFECT_STATUS=('NOT_SENT','SENT','UNKNOWN')
EFFECT_NEXT={'NOT_SENT':{'SENT','UNKNOWN'},'SENT':{'UNKNOWN'},'UNKNOWN':set()}
EFFECT_STEP=re.compile(r'[a-z][a-z0-9_-]{0,39}')

def describe_states():
    return deepcopy(STATE_ENTRY)

def next_actions(record):
    if record.get('state')=='PREPARED' and record.get('reason')=='ADAPTER_REQUIRED':
        return ['WAIT_FOR_A_REGISTERED_ADAPTER']
    return list({
      'PREPARED':['CONFIRM_AND_EXECUTE_THIS_PLAN'],
      'NEEDS_USER_ACTION':['COMPLETE_BUSINESS_FIELDS'],
      'AWAITING_OFFICIAL':['COMPLETE_THE_STEP_ON_THE_OFFICIAL_SITE','CHECK_RESULT'],
      'DISPATCHING':['WAIT_OR_CHECK_RESULT'],
      'UNKNOWN':['CHECK_RESULT_ONLY_DO_NOT_RESEND'],
      'PENDING_APPROVAL':['WAIT_FOR_APPROVAL','CHECK_RESULT'],
      'PENDING_PAYMENT':['COMPLETE_PAYMENT_ON_THE_OFFICIAL_SITE','CHECK_RESULT'],
      'SUCCEEDED':[],
      'FAILED':['PREPARE_AGAIN_IF_APPROPRIATE']}.get(record.get('state'),['CHECK_RESULT']))

def describe_actions(service_id):
    return [{'operation':op,'parameters_schema':deepcopy(c.get('input_schema',c['schema'])),'required_business_fields':list(c['required']),
        'adapter_version':c['version'],'enabled':c['enabled'] and action_enabled(service_id),'reason':(('READY' if action_enabled(service_id) else 'WRITES_DISABLED') if c['enabled'] else (c.get('disabled_reason') or 'ADAPTER_REQUIRED')),
        'request_sequence':c['request_sequence'],'prechecks':list(c['prechecks']),
        'critical_fields':c['critical_fields'],'risk_conditions':c['risk_conditions'],'reconciliation':c['reconciliation'],
        'reconciliation_scope':c.get('reconciliation_scope')}
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

def effect_rows(record):
    rows=record.get('effects') if isinstance(record.get('effects'),list) else []
    return [{'step':row.get('step'),'status':row.get('status')} for row in rows if isinstance(row,dict)]

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
            # After a renewal the loan lives at the new due date; the original
            # bound date must not make the todo refresh miss it.
            start=record['business'].get('date') or ''
            end=start
            new_due=(record.get('platform') or {}).get('new_due_at')
            if isinstance(new_due,str) and len(new_due)>=10 and new_due[:10]>end:
                end=new_due[:10]
            extra['todo_query']={'services':['library.'+record['business']['system']],
                                 'start_date':start,'end_date':end,'include_completed':True}
    history=[{'state':event.get('state'),'reason':event.get('reason'),'at':event.get('at')} for event in record.get('events',[]) if isinstance(event,dict)]
    result={**extra,'operation_id':record['operation_id'],'plan_id':record['operation_id'],'status':record['state'],
        'service_id':record['service_id'],'operation':record['operation'],'preview':fields,'expires_at':record['expires_at'],
        'missing_fields':record.get('missing_fields',[]),'reason':record.get('reason'),'adapter_version':record['adapter_version'],
        'verification_level':'REMOTE_RECORD_VERIFIED' if record['state'] in {'SUCCEEDED','PENDING_PAYMENT','PENDING_APPROVAL'} else 'LOCAL_EXECUTION_STATE',
        'record_reference':mask(record.get('record_id')),'authorization_recorded':bool(record.get('authorization')),
        'authorization_is_not_consent_proof':True,'automatic_resend':False,'revision':record['revision'],
        'effects':effect_rows(record),'next_actions':next_actions(record),
        'state_history':history[-20:],'event_count':len(history)}
    if record.get('challenge') and record['state']=='PREPARED' and record.get('reason')!='ADAPTER_REQUIRED':
        result['authorization_protocol']=HOST_AUTHORIZATION_VERSION
        result['local_confirmation_command']='python -m thu_assistant.consent --plan '+record['operation_id']
    return result

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

class SideEffectLedger:
    """Durable list of multi-stage side effects.

    Each step is recorded as definite not-sent, sent or unknown.  The ledger is
    opt-in for adapters (``accepts_ledger = True``) so existing adapters keep
    their exact ``dispatch(record, gate)`` signature.
    """
    def __init__(self,engine,operation_id):
        self.engine=engine;self.operation_id=operation_id
    def _mark(self,step,status):
        if not isinstance(step,str) or not EFFECT_STEP.fullmatch(step):raise CampusError('INVALID_ARGUMENTS')
        if status not in EFFECT_STATUS:raise CampusError('INVALID_ACTION_TRANSITION')
        def change(record):
            if record.get('state') in {'SUCCEEDED','FAILED'}:raise CampusError('INVALID_ACTION_TRANSITION')
            rows=record.setdefault('effects',[])
            row=next((value for value in rows if isinstance(value,dict) and value.get('step')==step),None)
            now=self.engine.clock()
            if row is None:
                if len(rows)>=50:raise CampusError('INVALID_ARGUMENTS')
                rows.append({'step':step,'status':status,'at':now,'history':[]});return
            current=row.get('status')
            if current==status:return
            if status not in EFFECT_NEXT.get(current,set()):raise CampusError('INVALID_ACTION_TRANSITION')
            row.setdefault('history',[]).append({'status':current,'at':row.get('at')})
            row.update(status=status,at=now)
        self.engine.store.update(self.operation_id,change)
    def declare(self,step):  # The step has definitely not been sent yet.
        self._mark(step,'NOT_SENT')
    def sent(self,step):     # The step was handed to the transport; result unknown.
        self._mark(step,'SENT')
    def unknown(self,step):  # A timeout or lost response; never replay.
        self._mark(step,'UNKNOWN')
    def snapshot(self):
        return effect_rows(self.engine.store.read(self.operation_id))

def normalized_attachments(business):
    value=business.get('attachments') if isinstance(business,dict) else None
    if not isinstance(value,list):return None
    rows=[]
    for item in value:
        if not isinstance(item,dict):return value
        rows.append((item.get('name'),item.get('sha256'),item.get('size')))
    return sorted(rows,key=lambda row:tuple('' if part is None else str(part) for part in row))

class ActionEngine:
    def __init__(self,store=None,account_provider=identity,adapters=None,consent_verifier=None,clock=time.time,host_consent=None):
        self.store=store or ActionStore();self.account_provider=account_provider;self.adapters=adapters or {}
        # consent_verifier is an in-process trusted-host callback used by the
        # existing Windows confirmation and synthetic adapters.  The signed
        # pending-grant store is always consulted as well; no MCP parameter,
        # web page, model output or ordinary environment variable reaches it.
        self.consent_verifier=consent_verifier
        self.host_consent=host_consent if host_consent is not None else HostConsent(self.store)
        self.clock=clock
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
            'missing_fields':missing,'reason':'MISSING_BUSINESS_FIELDS' if missing else None,'revision':0,'events':[],
            'challenge':new_challenge(),'consent_protocol':HOST_AUTHORIZATION_VERSION,'effects':[]}
        if not self.adapters.get((service,operation)):record['reason']='ADAPTER_REQUIRED' if not missing else 'MISSING_BUSINESS_FIELDS'
        self.event(record,record['state'],record['reason']);return public(self.store.create(record))
    def prepare_browser(self,payload,account,context):
        if account!=self.account_provider():raise CampusError('ACTION_ACCOUNT_MISMATCH')
        # Generic form fields stay encrypted; the generic form has no write contract.
        return self._prepare('browser','submit',payload,[], 'browser-preview-v1',context,account)
    def event(self,record,state,reason=None):
        events=record.setdefault('events',[])
        if events and state not in TRANSITIONS[record['state']]:raise CampusError('INVALID_ACTION_TRANSITION')
        last=events[-1] if events else None
        record.update(state=state,reason=reason)
        if last and last.get('state')==state and last.get('reason')==reason:
            return  # A repeated readback is not a new transition.
        events.append({'state':state,'reason':reason,'at':self.clock()})
    def ledger(self,operation_id):
        record=self.store.read(operation_id)
        if record.get('state')!='DISPATCHING' or record.get('owner_pid')!=os.getpid():
            raise CampusError('INVALID_ACTION_TRANSITION')
        return SideEffectLedger(self,operation_id)
    def _discard_grant(self,operation_id):
        if self.host_consent is None:return
        try:self.host_consent.discard(operation_id)
        except CampusError:pass  # Cleanup must not turn a decline or expiry into a different error.
    def _uncertain_effects(self,operation_id):
        return any(row['status'] in ('SENT','UNKNOWN') for row in effect_rows(self.store.read(operation_id)))
    def _is_native_prompt(self):
        verifier=self.consent_verifier
        return (getattr(verifier,'__module__',None)=='thu_assistant.sports_actions'
                and getattr(verifier,'__name__',None)=='native_grant')
    def _native_decline(self):
        """True only for the known Windows native prompt returning None.

        A callback that returns None means "no evidence" and must not fall back
        to a pending signed grant; only an explicit ``NOT_APPLICABLE`` (or the
        native prompt on a non-Windows host, where it does not apply) falls
        back.  The native message box uses None for an explicit "no".
        """
        return NATIVE_CONFIRMATION_DECLINES_ON_NONE and self._is_native_prompt()
    def _pending_evidence(self,record):
        """Return ``(grant, from_file)`` without consuming a pending file.

        ``DECLINED`` and the Windows native prompt returning ``None`` are
        explicit refusals and veto any pending signed grant.
        ``NOT_APPLICABLE`` and the native prompt on a non-Windows host fall back
        to the signed grant issued by the local consent CLI.  Any other
        callback returning ``None`` provides no evidence and does not fall back.
        """
        grant=None
        if self.consent_verifier is not None:
            grant=self.consent_verifier(deepcopy(record))
            if grant is DECLINED:
                self._discard_grant(record['operation_id'])
                raise CampusError('SPECIFIC_HUMAN_AUTHORIZATION_REQUIRED')
            if grant is NOT_APPLICABLE:
                grant=None
            elif grant is None:
                if self._native_decline():
                    self._discard_grant(record['operation_id'])
                    raise CampusError('SPECIFIC_HUMAN_AUTHORIZATION_REQUIRED')
                if not self._is_native_prompt():
                    raise CampusError('TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED')
        if grant is not None:
            grant=self._check_grant(record,grant)
            self._discard_grant(record['operation_id'])
            return grant,False
        grant=self.host_consent.peek(record,now=self.clock()) if self.host_consent is not None else None
        return (grant,True) if grant is not None else (None,False)
    def _check_grant(self,record,grant):
        expected={'plan_id':record['operation_id'],'account':record['account'],'digest':record['digest'],'adapter_version':record['adapter_version']}
        if not isinstance(grant,dict) or any(grant.get(k)!=v for k,v in expected.items()) or grant.get('source')!='human' or not grant.get('reference'):
            raise CampusError('TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED')
        expires=grant.get('expires_at')
        if isinstance(expires,bool) or not isinstance(expires,(int,float)):
            raise CampusError('TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED')
        if expires<=self.clock():raise CampusError('ACTION_PLAN_EXPIRED')
        version=grant.get('version')
        if version is None:
            # Compatibility for in-process confirmation callbacks (existing
            # Windows prompt and synthetic adapters).  A supplied challenge must
            # still match; this path is never reachable from MCP arguments.
            # The result is normalized into a signed, challenge-bound grant so
            # unsigned evidence is never stored or dispatched.
            if 'challenge' in grant and grant['challenge']!=record.get('challenge'):
                raise CampusError('TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED')
            issued=grant.get('issued_at')
            if issued is not None and (isinstance(issued,bool) or not isinstance(issued,(int,float)) or issued>self.clock()+60):
                raise CampusError('TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED')
            if self.host_consent is None:raise CampusError('TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED')
            try:remaining=min(float(expires),float(record['expires_at']))-self.clock()
            except (KeyError,TypeError,ValueError):raise CampusError('ACTION_LOG_CORRUPT') from None
            if remaining<=0:raise CampusError('ACTION_PLAN_EXPIRED')
            return self.host_consent.issue_for(record,now=self.clock(),
                ttl_seconds=max(1,int(min(300,remaining))),reference=grant['reference'])
        if version!=HOST_AUTHORIZATION_VERSION or self.host_consent is None:
            raise CampusError('TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED')
        self.host_consent.verify_signed(grant,record,now=self.clock())
        return grant
    def execute(self,plan_id,authorization):
        record=self.store.read(plan_id);self._bound(record)
        if record['state'] not in {'PREPARED','NEEDS_USER_ACTION'}:return self.result(plan_id)
        if record['state']=='NEEDS_USER_ACTION':return public(record)
        def invalidate(reason):
            def update(r):
                if r['state']!='PREPARED' or r['revision']!=record['revision']:raise CampusError('ACTION_ALREADY_CLAIMED')
                self.event(r,'FAILED',reason)
            result=public(self.store.update(plan_id,update,record['revision']))
            self._discard_grant(plan_id)
            return result
        if self.clock()>=record['expires_at']:
            return invalidate('ACTION_PLAN_EXPIRED')
        adapter=self._adapter(record)
        if not adapter:raise CampusError('ADAPTER_REQUIRED')
        if record['adapter_version']=='sports-action-v1' and not sports_enabled():raise CampusError('SPORTS_WRITES_DISABLED')
        if record['adapter_version']==library_actions.VERSION and not library_actions.enabled():raise CampusError('LIBRARY_WRITES_DISABLED')
        if not isinstance(authorization,str) or len(authorization.strip())<8:raise CampusError('SPECIFIC_HUMAN_AUTHORIZATION_REQUIRED')
        if not record.get('challenge'):
            # A record created before host-authorization-v1 has no challenge.
            # Add one locally; this changes no business field and no digest.
            def add_challenge(r):
                if not r.get('challenge'):
                    r['challenge']=new_challenge();r['consent_protocol']=HOST_AUTHORIZATION_VERSION
            record=self.store.update(plan_id,add_challenge,record['revision'])
        grant,from_file=self._pending_evidence(record)
        if grant is None:
            fresh=self.store.read(plan_id)
            if fresh['state']!='PREPARED':return self.result(plan_id)
            raise CampusError('TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED')
        try:
            fresh=adapter.preflight(deepcopy(record))
            handoff=fresh.get('official_handoff') if record['adapter_version'] in VERIFIED_ADAPTERS else None
            # Exact comparison of every business field, page context and any
            # attachment identity; an authenticator token may refresh, business
            # content may not change.
            changed=(digest(fresh['business'])!=record['digest'] or fresh.get('context')!=record['context']
                     or normalized_attachments(fresh['business'])!=normalized_attachments(record['business']))
            # Only the checks this operation actually declares are required; a
            # renewal plan has no availability/fee axis to verify.
            required_checks=REGISTRY.get((record['service_id'],record['operation']),{}).get('prechecks',CHECKS)
            if changed or (not handoff and any(fresh.get('checks',{}).get(k) is not True for k in required_checks)):
                return invalidate('PRECONDITIONS_CHANGED_PREPARE_AGAIN')
            gate=None if handoff else RequestGate(fresh['requests'])
        except CampusError:raise
        except Exception:raise CampusError('ACTION_PREFLIGHT_FAILED') from None
        self._bound(record)
        if from_file:
            # Consume the single-use grant only after a successful preflight, so
            # a transient browser/network failure does not destroy the human
            # confirmation.
            grant=self.host_consent.take(record,now=self.clock()) if self.host_consent is not None else None
            if grant is None:
                fresh=self.store.read(plan_id)
                if fresh['state']!='PREPARED':return self.result(plan_id)
                raise CampusError('TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED')
            grant=self._check_grant(record,grant)
        if self.clock()>=record['expires_at'] or self.clock()>=grant['expires_at']:raise CampusError('ACTION_PLAN_EXPIRED')
        def claim(r):
            if r['state']!='PREPARED' or r['revision']!=record['revision']:raise CampusError('ACTION_ALREADY_CLAIMED')
            if self.clock()>=r['expires_at'] or self.clock()>=grant['expires_at']:raise CampusError('ACTION_PLAN_EXPIRED')
            r['authorization']={'record':authorization,'evidence':grant};r['owner_pid']=os.getpid();r['dispatch_at']=self.clock()
            if handoff:self.event(r,'AWAITING_OFFICIAL',handoff)
            else:self.event(r,'DISPATCHING')
        try:record=self.store.update(plan_id,claim,record['revision'])
        except CampusError as e:
            if e.code in ('ACTION_ALREADY_CLAIMED','ACTION_RESULT_CHANGED_RECHECK'):return self.result(plan_id)
            raise
        if handoff:return public(record)
        # DISPATCHING is durable before the adapter receives the one-use request gate.
        ledger=self.ledger(plan_id)
        try:
            if getattr(adapter,'accepts_ledger',False):receipt=adapter.dispatch(deepcopy(record),gate,ledger)
            else:receipt=adapter.dispatch(deepcopy(record),gate)
            if record['adapter_version']==library_actions.VERSION and gate.sent==1 and receipt is True:
                self.store.update(plan_id,lambda r:r.update(platform_accepted=True))
        except NotDispatched:
            if gate.sent==0 and not self._uncertain_effects(plan_id):
                return public(self.store.update(plan_id,lambda r:self.event(r,'FAILED','REQUEST_NOT_SENT')))
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
        unchanged=(record.get('state')==state and record.get('reason')==reason
                   and (not remote or record.get('record_id')==remote))
        if unchanged:
            # A repeated poll with the same result must not rewrite the journal
            # or grow the public history.
            shown=public(record)
            if record.get('adapter_version') in VERIFIED_ADAPTERS:
                from datetime import datetime, timezone
                shown['checked_at']=datetime.now(timezone.utc).isoformat()
            return shown
        def update(r):
            if r['revision']!=revision:raise CampusError('ACTION_RESULT_CHANGED_RECHECK')
            self.event(r,state,reason)
            if remote:r['record_id']=remote
            if r['adapter_version'] in VERIFIED_ADAPTERS:
                from datetime import datetime, timezone
                r['checked_at']=datetime.now(timezone.utc).isoformat()
                r['platform']=matched[0].get('platform') if remote else None
        try:return public(self.store.update(oid,update,revision))
        except CampusError as error:
            if error.code=='ACTION_RESULT_CHANGED_RECHECK':return public(self.store.read(oid))
            raise
    def result(self,operation_id):
        record=self.store.read(operation_id);self._bound(record)
        if record['state']=='DISPATCHING':
            alive=process_alive(record.get('owner_pid'))
            if alive and self.clock()-record['dispatch_at']<120:return public(record)
            def interrupted(r):
                if r['state']=='DISPATCHING' and r['revision']==record['revision']:self.event(r,'UNKNOWN','DISPATCH_INTERRUPTED_RECONCILE_ONLY')
            try:record=self.store.update(operation_id,interrupted,record['revision'])
            except CampusError as error:
                if error.code=='ACTION_RESULT_CHANGED_RECHECK':record=self.store.read(operation_id)
                else:raise
        if record['state'] in {'UNKNOWN','PENDING_PAYMENT','PENDING_APPROVAL','AWAITING_OFFICIAL'}:
            adapter=self._adapter(record)
            if adapter:return self._reconcile(record,adapter)
        return public(record)
