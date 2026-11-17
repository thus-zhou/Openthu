"""Registered inputs and persisted preview fields for library transactions.

Seat and study-room create/cancel keep their original names and input fields.
Renewal is a separate, additive operation (``renew_loan``; 6.1 fixes the final
public name).  Its execution is registered as not enabled until a verified
source contract exists: the read side may produce an eligibility-bound plan and
an official-page handoff, but no guessed renewal request is ever dispatched.

``precheck_summary`` is the eight-axis projection persisted with one bound plan.
It is deliberately *not* the full 4.3 read object (which also carries resource,
window, items and sources).
"""
import os
from .queries import schema

VERSION = 'library-space-action-v1'
CREATE_SEAT = 'create_seat_reservation'
CREATE_ROOM = 'create_room_reservation'
CANCEL = 'cancel_reservation'
RENEW_LOAN = 'renew_loan'
ID = {'type':'string','pattern':r'[1-9][0-9]{0,15}'}
DATE = {'type':'string','pattern':r'\d{4}-\d{2}-\d{2}'}
CLOCK = {'type':'string','pattern':r'(?:[01]\d|2[0-3]):[0-5]\d'}
SLOT = {'date':DATE,'start_time':CLOCK,'end_time':CLOCK}
TEXT = {'type':'string','maxLength':500}
MEMBERS = {'type':'array','items':{'type':'string','minLength':1,'maxLength':80},'minItems':1,'maxItems':40}
# Loan ids are read from the official Primo loan row and are not guaranteed to
# be numeric; the pattern is the same tolerant id used by other record reads.
LOAN_ID = {'type':'string','pattern':r'[A-Za-z0-9_.:-]{1,64}'}
INPUTS = {
    CREATE_SEAT:{**SLOT,'area_id':ID,'seat_id':ID},
    CREATE_ROOM:{**SLOT,'kind_id':ID,'room_id':ID,'headcount':{'type':'integer','minimum':1,'maximum':40},
                 'members':MEMBERS,'purpose':{**TEXT,'minLength':1}},
    CANCEL:{'system':{'type':'string','pattern':'seat|room'},'date':DATE,'record_id':ID},
}
# Kept apart from INPUTS so the existing library adapter registration loop
# (owned by 6.1) keeps registering exactly the three legacy operations.
RENEWAL_INPUTS = {RENEW_LOAN:{'record_id':LOAN_ID}}
PRECHECK_KEYS = ('version','operation','selection_state','availability_state','eligibility_state',
                 'fees_state','rules_state','preparation_state')
PRECHECK = {'type':'object','additionalProperties':False,'required':list(PRECHECK_KEYS),
            'properties':{k:{'type':'string','maxLength':60} for k in PRECHECK_KEYS}}
PUBLIC = ('target','system','date','start_time','end_time','area_id','seat_id','kind_id','room_id',
          'headcount','record_id','cancel_rule','official_url','handoff','rules_summary',
          'due_at','new_due_at','renew_flag','renewal_state','precheck_summary',
          'contract_id','contract_evidence')
OUTPUTS = {k:{'type':'string','maxLength':4000} for k in
           ('target','system','date','start_time','end_time','location','cancel_rule','official_url','handoff',
            'rules_summary','proof','record_uuid','original_state','applicant','due_at','new_due_at',
            'renew_flag','renewal_state','renewal_hint','source','subject','contract_id','contract_digest',
            'contract_evidence','reader_account')}
OUTPUTS.update(resolved_members={'type':'array','items':ID,'maxItems':40},
               prior_ids={'type':'array','items':ID,'maxItems':2000},
               precheck_summary=PRECHECK)

SEQUENCE = {CREATE_SEAT:'ONE_BOUND_POST_OR_OFFICIAL_HANDOFF',
            CREATE_ROOM:'ONE_BOUND_POST_OR_OFFICIAL_HANDOFF',
            CANCEL:'ONE_BOUND_POST_OR_OFFICIAL_HANDOFF',
            RENEW_LOAN:'ONE_BOUND_POST_AFTER_A_VERIFIED_SOURCE_CONTRACT_OR_OFFICIAL_HANDOFF'}
# The three legacy operations keep the exact string they published in batch 4;
# the refined scope is additive so a client comparing the old value still sees it.
LEGACY_RECONCILIATION = 'OWN_RECORD_RESOURCE_TIME_MEMBERS_STATUS'
RECONCILIATION_SCOPE = {CREATE_SEAT:'OWN_RECORD_RESOURCE_TIME_STATUS',
                        CREATE_ROOM:'OWN_RECORD_RESOURCE_TIME_MEMBERS_STATUS',
                        CANCEL:'OWN_RECORD_RESOURCE_TIME_STATUS',
                        RENEW_LOAN:'OWN_LOAN_SAME_RECORD_AND_NEW_DUE_AT'}


def enabled():
    return all(os.environ.get(k,'1')!='0' for k in ('THU_LIBRARY_WRITE','THU_LIBRARY_READ','THU_SPACE_READ'))


def renewal_enabled():
    """Renewal has its own rollout switch; it never bypasses the library one."""
    return enabled() and os.environ.get('THU_LIBRARY_RENEW','1')!='0'


def precheck_states(block):
    """Project a full 4.3 precheck block onto the persisted eight-axis summary."""
    return {'version':str(block.get('version') or ''),
            'operation':str(block.get('operation') or ''),
            'selection_state':str((block.get('selection') or {}).get('state') or ''),
            'availability_state':str((block.get('availability') or {}).get('state') or ''),
            'eligibility_state':str((block.get('eligibility') or {}).get('state') or ''),
            'fees_state':str((block.get('fees') or {}).get('state') or ''),
            'rules_state':str((block.get('rules') or {}).get('state') or ''),
            'preparation_state':str(block.get('preparation_state') or '')}


def contracts():
    result={}
    for op,fields in INPUTS.items():
        result[('library',op)]={
            'version':VERSION,'input_schema':schema(fields),'schema':schema({**OUTPUTS,**fields}),'required':tuple(fields),
            'preview_fields':list(PUBLIC),'critical_fields':list(fields)+['proof','prior_ids'],
            'prechecks':('availability','eligibility','fees','rules'),
            'risk_conditions':['MEMBER_NOT_UNIQUE','SEAT_MAP_CHANGED','WINDOW_OR_RULE_CHANGED','REMOTE_RESULT_UNCERTAIN'],
            'request_sequence':SEQUENCE[op],'reconciliation':LEGACY_RECONCILIATION,
            'reconciliation_scope':RECONCILIATION_SCOPE[op],
            'enabled':True}
    for op,fields in RENEWAL_INPUTS.items():
        result[('library',op)]={
            'version':VERSION,'input_schema':schema(fields),'schema':schema({**OUTPUTS,**fields}),'required':tuple(fields),
            'preview_fields':list(PUBLIC),'critical_fields':list(fields)+['proof','due_at','subject'],
            # Renewal has no availability/fee axis to verify before dispatch.
            'prechecks':('eligibility','rules'),
            'risk_conditions':['RENEWAL_ELIGIBILITY_CHANGED','DUE_DATE_CHANGED','SOURCE_CONTRACT_UNVERIFIED',
                               'REMOTE_RESULT_UNCERTAIN'],
            'request_sequence':SEQUENCE[op],
            # Renewal is a new operation, so it has no legacy string to keep;
            # both fields carry the same string shape the legacy operations use
            # (the engine and 6.1 must not receive a dict where a descriptor is
            # expected).
            'reconciliation':RECONCILIATION_SCOPE[op],
            'reconciliation_scope':RECONCILIATION_SCOPE[op],
            # No verified renewal source contract is registered yet; the plan
            # can be prepared and handed to the official page but the engine
            # must not report this write operation as ready.
            'enabled':False,
            'disabled_reason':'SOURCE_CONTRACT_UNVERIFIED'}
    return result
