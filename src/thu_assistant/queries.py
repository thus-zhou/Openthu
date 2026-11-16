"""Registered read operations and bounded, process-local result snapshots."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from .bridge import CampusError
from . import catalog

VERSION='query-v1'
TEXT={'type':'string','maxLength':2000}
def schema(properties=None,required=()):
    return {'type':'object','properties':properties or {},'required':list(required),'additionalProperties':False}
PAGE=schema({'json_pointer':TEXT,'authenticate':{'type':'boolean'},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':100000}})
REGISTRY={
    ('*','page'):(PAGE,'snapshot_text_or_list','PAGE_READ'),
    ('*','status'):(schema(),'none','PAGE_READ'),
    ('campus','page'):(schema({**PAGE['properties'],'url':TEXT,'via_webvpn':{'type':'boolean'}},('url',)),'snapshot_text_or_list','PAGE_READ'),
    ('info','directory'):(schema({'query':TEXT,'category':TEXT}),'upstream_pages_then_snapshot','DIRECTORY_READ'),
    ('learn','courses'):(schema({'semester':{'type':'string','pattern':r'^[a-zA-Z0-9_-]{1,40}$'},'query':TEXT}),'upstream_full_list_then_snapshot','BUSINESS_READ'),
}
ID={'type':'string','pattern':r'^[a-zA-Z0-9_-]{1,128}$'}
COURSE={'course_id':ID,'semester':ID,'course_name':{'type':'string','minLength':1,'maxLength':240}}
for _operation,_schema in {
    'semesters':schema(), 'notices':schema(COURSE),
    'notice_detail':schema({**COURSE,'notice_id':ID},('notice_id',)),
    'assignments':schema(COURSE),
    'assignment_detail':schema({**COURSE,'assignment_id':ID},('assignment_id',)),
    'files':schema({**COURSE,'query':TEXT}),
    'calendar':schema({'start_date':{'type':'string','pattern':r'^\d{4}-\d{2}-\d{2}$'},
        'end_date':{'type':'string','pattern':r'^\d{4}-\d{2}-\d{2}$'},'graduate':{'type':'boolean'}},('start_date','end_date')),
}.items():REGISTRY[('learn',_operation)]=(_schema,'bounded_upstream_then_snapshot','BUSINESS_READ')

LIBRARY_RANGE={'offset':{'type':'integer','minimum':0,'maximum':100000},'limit':{'type':'integer','minimum':1,'maximum':50}}
LIBRARY_TERM={'type':'string','minLength':1,'maxLength':200}
LIBRARY_OPS={
    'catalog_search':schema({'query':LIBRARY_TERM,'field':{'type':'string','pattern':'keyword|title|author'},
                             'scope':{'type':'string','pattern':'all|print|online'},**LIBRARY_RANGE},('query',)),
    'catalog_detail':schema({'record_id':{'type':'string','pattern':r'[A-Za-z0-9_.:-]{1,250}'}},('record_id',)),
    'loans':schema(),
    'database_links':schema({'query':LIBRARY_TERM,'database_id':{'type':'string','pattern':r'[0-9]{1,30}'},**LIBRARY_RANGE}),
}
for _operation,_schema in LIBRARY_OPS.items():
    REGISTRY[('library',_operation)]=(_schema,'upstream_window_then_snapshot' if _operation in ('catalog_search','database_links') else 'snapshot_list','BUSINESS_READ')

SPACE_ID={'type':'string','pattern':r'[1-9][0-9]{0,15}'}
SPACE_DATE={'type':'string','pattern':r'\d{4}-\d{2}-\d{2}'}
SPACE_TIME={'type':'string','pattern':r'(?:[01][0-9]|2[0-3]):[0-5][0-9]'}
SPACE_WINDOW={'date':SPACE_DATE,'start_time':SPACE_TIME,'end_time':SPACE_TIME,'name':LIBRARY_TERM}
SPACE_PAGE={'page':{'type':'integer','minimum':1,'maximum':10000},'limit':{'type':'integer','minimum':1,'maximum':50}}
SPACE_OPS={
    'seat_areas':schema({'parent_id':SPACE_ID,'query':LIBRARY_TERM}),
    'seat_availability':schema({'area_id':SPACE_ID,**SPACE_WINDOW}),
    'room_types':schema({'query':LIBRARY_TERM}),
    'room_availability':schema({'kind_id':SPACE_ID,**SPACE_WINDOW,**SPACE_PAGE,'people':{'type':'integer','minimum':1,'maximum':500}}),
    'reservations':schema({'system':{'type':'string','pattern':'seat|room'},**SPACE_PAGE,'start_date':SPACE_DATE,'end_date':SPACE_DATE,
                            'status_mask':{'type':'integer','minimum':1,'maximum':65535}},('system',)),
}
for _operation,_schema in SPACE_OPS.items():
    REGISTRY[('library',_operation)]=(_schema,'upstream_window_then_snapshot' if _operation in ('room_availability','reservations') else 'snapshot_list','BUSINESS_READ')
from . import sports
SPORTS_OPS = {
    'scenes': schema({'query': LIBRARY_TERM}),
    'availability': schema({'scene_id': sports.ID, 'name': LIBRARY_TERM, 'room_id': sports.ID,
                            'date': sports.DAY, 'mode': {'type':'string','pattern':'SPORT_GROUP|SPORT_PERSON'}, **sports.WINDOW}),
    'orders': schema(sports.WINDOW),
    'order_detail': schema({'record_id': sports.ID}, ('record_id',)),
}
for _op, _schema in SPORTS_OPS.items():
    REGISTRY[('sports',_op)] = (_schema,'upstream_window_then_snapshot','BUSINESS_READ')

def now():return datetime.now(timezone.utc).isoformat()
def validate(value,spec):
    kind=spec.get('type')
    valid={'array':isinstance(value,list),'object':isinstance(value,dict),'string':isinstance(value,str),'integer':type(value) is int,'boolean':type(value) is bool}
    if kind not in valid or not valid[kind]:raise CampusError('INVALID_ARGUMENT_TYPE')
    if kind=='array':
        if not spec.get('minItems',0)<=len(value)<=spec.get('maxItems',1000):raise CampusError('INVALID_ARGUMENT_LENGTH')
        for item in value:validate(item,spec['items'])
    if kind=='object':
        if set(value)-set(spec.get('properties',{})):raise CampusError('INVALID_ARGUMENTS')
        if any(k not in value for k in spec.get('required',[])):raise CampusError('MISSING_ARGUMENT')
        for k,v in value.items():validate(v,spec['properties'][k])
    if kind=='string':
        if not spec.get('minLength',0)<=len(value)<=spec.get('maxLength',1000000):raise CampusError('INVALID_ARGUMENT_LENGTH')
        if spec.get('pattern') and not re.fullmatch(spec['pattern'],value):raise CampusError('INVALID_ARGUMENT_VALUE')
    if kind=='integer' and not spec.get('minimum',value)<=value<=spec.get('maximum',value):raise CampusError('INVALID_ARGUMENT_RANGE')

def describe(service_id):
    if service_id!='campus':catalog.resolve(service_id)
    operations=[]
    for operation in ('page','status','directory','courses','semesters','notices','notice_detail','assignments','assignment_detail','files','calendar',*LIBRARY_OPS,*SPACE_OPS,*SPORTS_OPS):
        if operation in SPORTS_OPS and os.environ.get('THU_SPORTS_READ','1')=='0':continue
        if operation in (*LIBRARY_OPS,*SPACE_OPS) and os.environ.get('THU_LIBRARY_READ','1')=='0':
            continue
        if operation in SPACE_OPS and os.environ.get('THU_SPACE_READ','1')=='0':
            continue
        entry=REGISTRY.get((service_id,operation)) or (REGISTRY.get(('*',operation)) if service_id!='campus' else None)
        if entry:
            parameters,pagination,level=entry
            expanded=service_id=='learn' and operation not in ('page','status','courses')
            operations.append({'operation':operation,'parameters_schema':deepcopy(parameters),'category':'read','pagination':pagination,
                'adapter_version':'learn-read-v1' if expanded else VERSION,'verification_level_ceiling':level,
                'last_acceptance':{'level':'LIVE_BUSINESS_READ' if expanded else {('info','directory'):'LIVE_PARTIAL_DIRECTORY',('learn','courses'):'LIVE_BUSINESS_READ'}.get((service_id,operation),'OFFLINE_CONTRACT'),
                    'date':'2026-10-31' if expanded else '2026-10-23','report':'06-验收.md' if expanded else '04-验收.md'},
                'note':'页面或目录命中不证明业务权限；本次证据在查询结果中返回。'})
            if service_id=='sports' and operation in SPORTS_OPS:
                operations[-1].update(adapter_version=sports.VERSION,last_acceptance={'report':'11-验收.md','level':'SEE_OPERATION_EVIDENCE'},note='余量按选定日期、模式和房间查询；下一来源页看 next_page。')
            if service_id=='library' and operation in LIBRARY_OPS:
                operations[-1].update(adapter_version='library-read-v1',last_acceptance={'date':'2026-11-03','report':'07-验收.md','level':'SEE_OPERATION_EVIDENCE'},
                    note='搜索返回所选 offset/limit 区间，next_offset 用于后续来源区间。数据库导航链接仅证明入口已登记。')
            if service_id=='library' and operation in SPACE_OPS:
                operations[-1].update(adapter_version='space-read-v1',last_acceptance={'date':'2026-11-05','report':'08-验收.md','level':'SEE_OPERATION_EVIDENCE'},
                    note='每次新查询实时读取；游标仅为同一检查时间的快照。next_page 表示下一来源页，complete 只覆盖当前来源页。空闲不代表具备预约资格。')
    from .actions import describe_actions
    return {'service_id':service_id,'operations':operations,'actions':describe_actions(service_id),'registry_version':VERSION}

SECRET=re.compile(r'password|cookie|token|csrf|ticket|session|signature|nonce|saml|relaystate|authorization|(?:^|_)sign$',re.I)
def safe_url(url):
    p=urlsplit(url or '')
    return urlunsplit((p.scheme,p.hostname or '',re.sub(r';[^/]*session[^/]*','',p.path,flags=re.I),urlencode([(k,v) for k,v in parse_qsl(p.query) if not SECRET.search(k)]),''))

def pointer(data,path):
    if path=='':return data
    if not path.startswith('/') or re.search(r'~(?![01])',path):raise CampusError('INVALID_JSON_POINTER')
    node=data
    for part in path[1:].split('/'):
        key=part.replace('~1','/').replace('~0','~')
        if isinstance(node,dict) and key in node:node=node[key]
        elif isinstance(node,list) and re.fullmatch(r'0|[1-9][0-9]*',key) and int(key)<len(node):node=node[int(key)]
        else:raise CampusError('JSON_POINTER_NOT_FOUND')
    return node

class Snapshots:
    def __init__(self,clock=time.monotonic,ttl=600,max_bytes=32*1024*1024,max_entries=64):
        self.clock=clock;self.ttl=ttl;self.max_bytes=max_bytes;self.max_entries=max_entries;self.entries={};self.cursors={}
    def purge(self):
        expired=[k for k,v in self.entries.items() if self.clock()>=v['expires']]
        for k in expired:self.drop(k)
    def drop(self,key):
        self.entries.pop(key,None)
        self.cursors={k:v for k,v in self.cursors.items() if v[0]!=key}
    def start(self,binding,result,kind,value,size,start=0):
        self.purge();payload=deepcopy(value);metadata=deepcopy(result)
        cost=len(json.dumps([payload,metadata],ensure_ascii=False).encode())
        if cost>self.max_bytes:raise CampusError('RESULT_TOO_LARGE_SELECT_NARROWER_SOURCE')
        while self.entries and (len(self.entries)>=self.max_entries or sum(v['bytes'] for v in self.entries.values())+cost>self.max_bytes):self.drop(next(iter(self.entries)))
        sid=secrets.token_urlsafe(24)
        self.entries[sid]={'binding':binding,'result':metadata,'kind':kind,'value':payload,'size':size,'expires':self.clock()+self.ttl,'bytes':cost,'tokens':{}}
        return self.page(sid,start)
    def resume(self,cursor,binding):
        self.purge()
        if cursor not in self.cursors:raise CampusError('CURSOR_EXPIRED')
        sid,index=self.cursors[cursor];entry=self.entries[sid]
        if entry['binding']!=binding:raise CampusError('CURSOR_QUERY_MISMATCH')
        return self.page(sid,index)
    def page(self,sid,index):
        e=self.entries[sid];out=deepcopy(e['result']);kind=e['kind'];value=e['value']
        if kind in ('list','text'):
            end=min(index+e['size'],len(value));chunk=deepcopy(value[index:end]);more=end<len(value)
            if more:
                if end not in e['tokens']:
                    token=secrets.token_urlsafe(24);e['tokens'][end]=token;self.cursors[token]=(sid,end)
                out['next_cursor']=e['tokens'][end]
            else:out['next_cursor']=None
            out.update(snapshot_complete=not more,complete=not more and out.get('source_complete') is True)
            if kind=='list':out.update(items=chunk,data=chunk,total_items=len(value),returned_items=len(chunk),item_offset=index)
            else:out.update(text=chunk,data=chunk,totalCharacters=len(value),offset=index,nextOffset=end if more else None)
        else:out.update(data=deepcopy(value),next_cursor=None,snapshot_complete=True,complete=out.get('source_complete') is True)
        return out

def binding(service,operation,parameters,page_size):
    return hashlib.sha256(json.dumps([service,operation,parameters,page_size],sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def metadata(service,operation,url='',level='NONE',status='UNKNOWN',complete=False,warnings=None,checked_at=None):
    return {'status':status,'source':{'service_id':service,'operation':operation,'url':safe_url(url),'adapter_version':VERSION},
        'checked_at':checked_at or now(),'verification_level':level,'source_complete':complete,'complete':complete,
        'next_cursor':None,'warnings':warnings or [],'missing_pages':[]}

def page_metadata(raw,service,operation):
    http=raw.get('httpStatus',0);auth=raw.get('authentication_state');old=raw.get('status')
    if http==429:status='RATE_LIMITED'
    elif http==403 or auth=='FORBIDDEN':status='FORBIDDEN'
    elif http==401 or auth in ('EXPIRED','LOGIN_REQUIRED') or old=='AUTH_REQUIRED':status='AUTH_REQUIRED'
    elif raw.get('maintenance'):status='MAINTENANCE'
    elif http>=400:status='HTTP_ERROR'
    elif old=='BROWSER_REQUIRED':status='BROWSER_REQUIRED'
    elif auth=='AUTHENTICATED':status='AUTHENTICATED'
    elif old=='PAGE_READ_VERIFIED':status='PUBLIC_READABLE' if raw.get('public_read') else 'READABLE_AUTH_UNKNOWN'
    else:status='UNKNOWN'
    level='AUTHENTICATED_PAGE' if status=='AUTHENTICATED' else 'PUBLIC_PAGE' if status=='PUBLIC_READABLE' else 'PAGE_READ' if status=='READABLE_AUTH_UNKNOWN' else 'TRANSPORT_READ' if old=='JSON_READ_VERIFIED' and 200<=http<300 else 'NONE'
    is_json='data' in raw
    complete=status in ('AUTHENTICATED','PUBLIC_READABLE','READABLE_AUTH_UNKNOWN') and not is_json
    warnings=['GENERIC_JSON_BUSINESS_STATUS_UNVERIFIED','UPSTREAM_COMPLETENESS_UNKNOWN'] if is_json else []
    if old=='BROWSER_REQUIRED':warnings.append('DYNAMIC_CONTENT_REQUIRES_BROWSER')
    return metadata(service,operation,raw.get('url'),level,status,complete,warnings,raw.get('checked_at'))

class QueryEngine:
    def __init__(self,owner):self.owner=owner;self.snapshots=Snapshots()
    def query(self,service_id,operation,parameters,cursor=None,page_size=50):
        ops={o['operation']:o for o in describe(service_id)['operations']}
        if operation not in ops:raise CampusError('UNREGISTERED_QUERY_OPERATION')
        validate(parameters,ops[operation]['parameters_schema']);validate(page_size,{'type':'integer','minimum':1,'maximum':200})
        key=binding(service_id,operation,parameters,page_size)
        if (service_id=='library' and operation in ('loans',*SPACE_OPS)) or service_id=='sports' and operation in SPORTS_OPS:
            key=binding(service_id,operation,{'query_key':key,'account':self.owner.bridge('action_identity')['account_id']},page_size)
        if cursor:return self.snapshots.resume(cursor,key)
        if operation in ('page','status'):
            args={**parameters,**({'service_id':service_id} if service_id!='campus' else {})}
            return self.read_page(args,page_size=page_size,bound=key,normalized=True,operation=operation)
        if (service_id=='library' and operation in (*LIBRARY_OPS,*SPACE_OPS)) or service_id=='sports' and operation in SPORTS_OPS:
            if service_id=='sports':
                library=sports
            elif operation in SPACE_OPS:
                from . import spaces as library
            else:
                from . import library
            if operation=='database_links' and (('database_id' in parameters and len(parameters)>1) or (set(parameters)&{'offset','limit'} and 'query' not in parameters)):
                raise CampusError('INVALID_LIBRARY_SELECTOR')
            raw=library.query(self.owner.browsers,operation,parameters)
            items=raw.pop('items')
            result=metadata(service_id,operation,raw.pop('url'),raw.pop('level','BUSINESS_READ'),
                            'OK' if raw['complete'] else 'PARTIAL',raw['complete'],raw.pop('warnings'))
            result['source']['adapter_version']=library.VERSION
            result.update(raw)
        elif operation=='directory':
            raw=self.owner.bridge('discover');items=raw['services']
            for field in ('query','category'):
                if parameters.get(field):items=[r for r in items if parameters[field].casefold() in (json.dumps(r,ensure_ascii=False) if field=='query' else r.get('category','')).casefold()]
            result=metadata(service_id,operation,'https://info.tsinghua.edu.cn/f/info/portal_fg/student/yyfwxxindex','DIRECTORY_READ',
                'OK' if raw['complete'] else 'PARTIAL',raw['complete'],raw.get('warnings',[]),raw.get('checkedAt'))
            result.update(upstream_total=raw['expected'],upstream_received=raw['count'],upstream_row_count=raw.get('rowCount'),
                deduplicated_entries=raw.get('duplicateCount',0),upstream_total_unit='directory_rows_before_deduplication',
                upstream_pages=raw.get('pages'),missing_pages=raw.get('missing_pages',[]),page_errors=raw.get('page_errors',[]))
        elif service_id=='learn' and operation!='courses':
            raw=self.owner.learning_call('query_learning',{'operation':operation,'parameters':parameters});items=raw['items']
            result=metadata('learn',operation,'https://zhjw.cic.tsinghua.edu.cn/jxmh_out.do' if operation=='calendar' else 'https://learn.tsinghua.edu.cn/',
                'BUSINESS_READ',raw.get('status','OK' if raw['complete'] else 'PARTIAL'),raw['complete'],raw.get('warnings',[]),raw.get('checked_at'))
            result['source']['adapter_version']='learn-read-v1'
            result.update(upstream_pages=raw.get('pages',[]),source_errors=raw.get('errors',[]),scope=raw.get('scope','selected Learn courses and operation'))
            for field in ('candidates','reason'):
                if field in raw:result[field]=raw[field]
        else:
            raw=self.owner.learning_call('list_courses',parameters);items=raw['courses']
            result=metadata('learn','courses','https://learn.tsinghua.edu.cn/b/wlxt/kc/v_wlkc_xs_xkb_kcb_extend/student/loadCourseBySemesterId/','BUSINESS_READ','OK',True)
            result['upstream_total']=None if parameters.get('query') else len(items)
            result['total_scope']='filtered_result' if parameters.get('query') else 'semester_courses'
        if not items and result['source_complete'] and result['status']=='OK':result['status']='EMPTY'
        return self.snapshots.start(key,result,'list',items,page_size)
    def read_page(self,args,page_size=50,bound=None,normalized=False,operation='page'):
        args=dict(args);cursor=args.pop('cursor',None);page_size=args.pop('page_size',page_size)
        validate(page_size,{'type':'integer','minimum':1,'maximum':200})
        service=args.get('service_id','campus');key=bound or binding(service,'read_page',args,page_size)
        if cursor:return self.snapshots.resume(cursor,key)
        offset=args.pop('offset',0);limit=args.pop('limit',24000);selected=args.pop('json_pointer',None)
        raw=self.owner.bridge('read',**catalog.request_arguments(**args),snapshot=True)
        result=page_metadata(raw,service,operation)
        result={**raw,**result} if normalized else {**result,**raw,'service_state':result['status']}
        result['checkedAt']=result['checked_at']
        if 'data' in raw:
            value=pointer(raw['data'],selected or '')
            result.pop('data',None);result['json_pointer']=selected or '';result['upstream_total']=None
            return self.snapshots.start(key,result,'list' if isinstance(value,list) else 'data',value,page_size)
        if selected is not None:raise CampusError('JSON_POINTER_REQUIRES_JSON')
        value=raw.get('text','');result.pop('text',None)
        return self.snapshots.start(key,result,'text',value,limit,offset)

def legacy_metadata(name,result):
    """Add evidence to old fields without promising unimplemented pagination."""
    if name=='list_courses':return {**metadata('learn','courses','https://learn.tsinghua.edu.cn/','BUSINESS_READ','EMPTY' if not result.get('count') else 'OK',True),**result}
    if name in ('list_assignments','list_course_files','get_assignment'):
        evidence=result.get('read_evidence')
        if evidence:
            out=metadata('learn',name,'https://learn.tsinghua.edu.cn/','BUSINESS_READ',
                ('EMPTY' if result.get('count')==0 else 'OK') if evidence['complete'] else 'PARTIAL',
                evidence['complete'],evidence.get('warnings',[]),evidence.get('checked_at'))
            out['source']['adapter_version']=evidence['adapter_version']
            return {**out,**result,'source_errors':evidence.get('errors',[]),'upstream_pages':evidence.get('pages',[])}
        return {**metadata('learn',name,'https://learn.tsinghua.edu.cn/','BUSINESS_READ','PARTIAL',False,['UPSTREAM_COMPLETENESS_UNVERIFIED']),**result}
    return result
