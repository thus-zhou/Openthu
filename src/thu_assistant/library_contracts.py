"""Registered inputs and persisted preview fields for library space transactions."""
import os
from .queries import schema

VERSION = 'library-space-action-v1'
CREATE_SEAT = 'create_seat_reservation'
CREATE_ROOM = 'create_room_reservation'
CANCEL = 'cancel_reservation'
ID = {'type':'string','pattern':r'[1-9][0-9]{0,15}'}
DATE = {'type':'string','pattern':r'\d{4}-\d{2}-\d{2}'}
CLOCK = {'type':'string','pattern':r'(?:[01]\d|2[0-3]):[0-5]\d'}
SLOT = {'date':DATE,'start_time':CLOCK,'end_time':CLOCK}
TEXT = {'type':'string','maxLength':500}
MEMBERS = {'type':'array','items':{'type':'string','minLength':1,'maxLength':80},'minItems':1,'maxItems':40}
INPUTS = {
    CREATE_SEAT:{**SLOT,'area_id':ID,'seat_id':ID},
    CREATE_ROOM:{**SLOT,'kind_id':ID,'room_id':ID,'headcount':{'type':'integer','minimum':1,'maximum':40},
                 'members':MEMBERS,'purpose':{**TEXT,'minLength':1}},
    CANCEL:{'system':{'type':'string','pattern':'seat|room'},'date':DATE,'record_id':ID},
}
PUBLIC = ('target','system','date','start_time','end_time','area_id','seat_id','kind_id','room_id',
          'headcount','record_id','cancel_rule','official_url','handoff','rules_summary')
OUTPUTS = {k:{'type':'string','maxLength':4000} for k in
           ('target','system','date','start_time','end_time','location','cancel_rule','official_url','handoff',
            'rules_summary','proof','record_uuid','original_state','applicant')}
OUTPUTS.update(resolved_members={'type':'array','items':ID,'maxItems':40},
               prior_ids={'type':'array','items':ID,'maxItems':2000})


def enabled():
    return all(os.environ.get(k,'1')!='0' for k in ('THU_LIBRARY_WRITE','THU_LIBRARY_READ','THU_SPACE_READ'))


def contracts():
    return {('library',op):{
        'version':VERSION,'input_schema':schema(fields),'schema':schema({**OUTPUTS,**fields}),'required':tuple(fields),
        'preview_fields':list(PUBLIC),'critical_fields':list(fields)+['proof','prior_ids'],
        'prechecks':('availability','eligibility','fees','rules'),
        'risk_conditions':['MEMBER_NOT_UNIQUE','SEAT_MAP_CHANGED','WINDOW_OR_RULE_CHANGED','REMOTE_RESULT_UNCERTAIN'],
        'request_sequence':'ONE_BOUND_POST_OR_OFFICIAL_HANDOFF','reconciliation':'OWN_RECORD_RESOURCE_TIME_MEMBERS_STATUS',
        'enabled':True} for op,fields in INPUTS.items()}

