from __future__ import annotations
import json
import os
import uuid
from datetime import datetime,timezone
from pathlib import Path
from .bridge import CampusError, gateway

def state_root():
    return Path(os.environ.get('THU_ASSISTANT_STATE_DIR') or Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'TsinghuaAssistant')

def directory_path():
    return state_root() / 'directory.json'

def load_services():
    static = json.loads(Path(__file__).with_name('services.json').read_text(encoding='utf-8'))
    try:
        dynamic = json.loads(directory_path().read_text(encoding='utf-8')).get('services', [])
    except (OSError, ValueError):
        dynamic = []
    by_roaming={row.get('roaming_id'):row for row in static if row.get('roaming_id')}
    remaining=[]
    for row in dynamic:
        alias=by_roaming.get(row.get('roaming_id'))
        if alias:
            alias['portal_id']=row['id']
            alias['description']=row.get('description','')
            alias['department']=row.get('department','')
        else:remaining.append(row)
    return static + remaining

def list_services(query: str | None = None, category: str | None = None):
    rows = load_services()
    if query:
        q = query.casefold()
        rows = [r for r in rows if q in ' '.join(str(r.get(k,'')) for k in ('id','name','description','category')).casefold()]
    if category:
        rows = [r for r in rows if category.casefold() in r.get('category','').casefold()]
    try:record=json.loads(directory_path().read_text(encoding='utf-8'))
    except (OSError,ValueError):record={}
    warnings=list(record.get('warnings',[])) if record else ['LIVE_DIRECTORY_NOT_CHECKED']
    if record and record.get('query_schema')!=1:warnings.append('CACHE_COMPLETENESS_NOT_REVALIDATED')
    return {'services': rows, 'count': len(rows),'status':'LOCAL_CATALOG_READ',
            'source':{'kind':'static_and_cached_directory','cached_at':record.get('checkedAt')},'checked_at':datetime.now(timezone.utc).isoformat(),
            'complete':record.get('query_schema')==1 and record.get('complete',False),'next_cursor':None,'warnings':warnings,
            'verification_level':'DIRECTORY_CACHE','scope': 'any HTTPS tsinghua.edu.cn subdomain is also accepted by read_page',
            'coverage': 'Directory membership does not prove login, permission or booking capability.'}

def refresh_directory():
    result = gateway('discover')
    result['query_schema']=1
    target = directory_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, target)
    return {'status': result['status'], 'count': result['count'], 'expected': result['expected'],
            'rowCount':result.get('rowCount'),'duplicateCount':result.get('duplicateCount'),
            'complete': result['complete'], 'checkedAt': result['checkedAt'],'checked_at':result['checkedAt'],
            'source':{'url':'https://info.tsinghua.edu.cn/f/info/portal_fg/student/yyfwxxindex'},'next_cursor':None,
            'warnings':result.get('warnings',[]),'missing_pages':result.get('missing_pages',[]),'page_errors':result.get('page_errors',[]),'verification_level':'DIRECTORY_READ'}

def resolve(service_id: str):
    found = next((row for row in load_services() if row['id'] == service_id or row.get('portal_id') == service_id), None)
    if not found:
        raise CampusError('SERVICE_NOT_FOUND_REFRESH_DIRECTORY_OR_USE_URL')
    return dict(found)

def request_arguments(service_id=None, url=None, via_webvpn=False, authenticate=True):
    if bool(service_id) == bool(url):
        raise CampusError('PROVIDE_ONE_SERVICE_ID_OR_URL')
    if url:
        return {'url': url, 'via_webvpn': via_webvpn, 'authenticate': authenticate}
    service = resolve(service_id)
    result = {'via_webvpn': via_webvpn or service.get('via_webvpn', False), 'authenticate': authenticate}
    if service.get('roaming_id'):
        result['roaming_id'] = service['roaming_id']
        if service.get('entry_url'):result['landing_url']=service['entry_url']
    else:
        result['url'] = service['entry_url']
    return result
