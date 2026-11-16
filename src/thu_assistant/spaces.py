"""Official library seat and room reads. Each query owns a short-lived browser.
No booking, cancellation, check-in or arbitrary API dispatch is exposed here.
"""
from datetime import date, datetime, timedelta, timezone
import re
from urllib.parse import urlencode, urlsplit
from .bridge import CampusError
from .library import plain

VERSION = 'space-read-v1'
SEAT = 'https://seat.lib.tsinghua.edu.cn'
ROOM = 'https://cab.lib.tsinghua.edu.cn'
TZ = timezone(timedelta(hours=8))
NUM = r'[1-9][0-9]{0,15}'
CLOCK = r'(?:[01][0-9]|2[0-3]):[0-5][0-9]'
RULE_KEYS = ('ruleName','earliestResvTime','latestResvTime','minResvTime','maxResvTime',
             'cancelTime','timeInterval','resvBeforeNoticeTime','resvAfterNoticeTime',
             'resvEndNewTime','earlyInTime','memo','limit')
SEAT_STATUS = {1:'FREE',2:'RESERVED',3:'LOCKED',4:'MAINTENANCE',5:'CLEANING',
               6:'IN_USE',7:'TEMPORARILY_AWAY',8:'ENDING_SOON',9:'TIME_EXPIRED'}

def require(ok, code='SPACE_RESPONSE_INVALID'):
    if not ok:
        raise CampusError(code)

def ident(value):
    require(re.fullmatch(NUM,str(value or '')) is not None)
    return str(value)

def day(value=None):
    try:
        return date.fromisoformat(value) if value else datetime.now(TZ).date()
    except (ValueError,TypeError):
        raise CampusError('INVALID_DATE') from None

def clock(value):
    require(isinstance(value,str) and re.fullmatch(CLOCK,value), 'INVALID_TIME')
    return value

def times(params):
    require(('start_time' in params)==('end_time' in params),'TIME_PAIR_REQUIRED')
    if 'start_time' not in params:
        return None
    a,b=clock(params['start_time']),clock(params['end_time'])
    require(a<b,'INVALID_TIME_RANGE')
    return a,b

def stamp(value):
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        try:return datetime.fromtimestamp(value/1000,TZ).isoformat()
        except (ValueError,OSError,OverflowError):pass
    if isinstance(value,str):
        try:return datetime.fromisoformat(value).replace(tzinfo=TZ).isoformat()
        except ValueError:pass
    raise CampusError('SPACE_TIME_INVALID')

def result(items,url,**extra):
    out=dict(items=items,url=url,complete=True,warnings=[],timezone='Asia/Shanghai',
             freshness='LIVE_AT_CHECKED_AT',reusable_for_booking=False)
    out.update(extra)
    return out

def check(payload,system):
    require(isinstance(payload,dict))
    status=payload.get('status' if system=='seat' else 'code')
    message=str(payload.get('msg' if system=='seat' else 'message',''))
    if status in (401,403) or re.search('未登录|没有登录|登录.*(?:超时|过期|失效)|未登陆|未认证',message):
        raise CampusError('FORBIDDEN' if status==403 else 'AUTH_REQUIRED')
    if type(status) is not int or status!=(1 if system=='seat' else 0):
        raise CampusError('SPACE_QUERY_REJECTED')
    data=payload.get('data')
    require(data is not None)
    return data

def seat_list(payload):
    data=check(payload,'seat')
    require(isinstance(data,dict) and 'list' in data)
    return data['list']

# Fixed reads, no caller-supplied URL, headers or arbitrary script.
SEAT_FETCH = """async path => {
 if (!(location.hostname==='seat.lib.tsinghua.edu.cn' ||
       location.hostname==='webvpn.tsinghua.edu.cn') || typeof jQuery==='undefined')
   throw new Error('WRONG_CONTEXT');
 return await new Promise((resolve,reject) => jQuery.ajax({
   url:path,type:'GET',dataType:'json',timeout:20000,
   success:resolve,error:()=>reject(new Error('SPACE_READ_FAILED'))}));
}"""
ROOM_FETCH = """async path => {
 if (location.origin!=='https://cab.lib.tsinghua.edu.cn') return {code:401};
 const user=JSON.parse(sessionStorage.getItem('userInfo')||'null');
 if (!user?.token) return {code:401};
 const r=await fetch(path,{method:'GET',redirect:'error',credentials:'same-origin',
   signal:AbortSignal.timeout(20000),headers:{token:user.token,lan:'1'}});
 return r.ok ? r.json() : {code:r.status};
}"""

def allowed_read(system,path,params):
    keys=set(params)
    if system=='seat':
        return ((re.fullmatch(r'/api.php/(?:v3areas|space_days|v3areadays)/'+NUM,path) is not None and not keys)
                or path=='/api.php/space_time_buckets' and keys=={'area','day'}
                or path=='/api.php/spaces_old' and keys=={'area','segment','day','startTime','endTime'})
    return (path in ('/ic-web/roomMenu','/ic-web/reserve/punishInfo') and not keys
            or path=='/ic-web/reserve' and keys=={'sysKind','resvDates','kindIds','page','pageSize','labId'}
            or path=='/ic-web/reserve/resvInfo' and keys=={'beginDate','endDate','needStatus','page','pageNum','orderKey','orderModel'})

class Reader:
    def __init__(self,browsers,system):
        self.browsers,self.system,self.sid=browsers,system,None
    def __enter__(self):
        try:
            opened=self.browsers.open(**({'url':SEAT+'/cas/index.php'} if self.system=='seat' else {'service_id':'library-rooms'}),visible=False)
            self.sid=opened['session_id']
            require(opened['status']!='TWO_FACTOR_REQUIRED','TWO_FACTOR_REQUIRED')
            self.page=self.browsers.sessions[self.sid]['page']
            u=urlsplit(self.page.url)
            if self.system=='seat':
                require(u.scheme=='https' and u.hostname in ('seat.lib.tsinghua.edu.cn','webvpn.tsinghua.edu.cn'),'AUTH_REQUIRED')
                if u.hostname=='webvpn.tsinghua.edu.cn':
                    require(re.fullmatch(r'/https/[0-9a-f]+/home/web/f_second',u.path) is not None,'AUTH_REQUIRED')
                    prefix=u.path[:-len('/home/web/f_second')]
                else:
                    prefix=''
                self.base=u.scheme+'://'+u.netloc+prefix
                require(self.page.evaluate("!!(window.ska?.access_token && window.ska?.userid)"),'AUTH_REQUIRED')
            else:
                require(u.scheme=='https' and u.hostname=='cab.lib.tsinghua.edu.cn','AUTH_REQUIRED')
                self.page.wait_for_function("!!JSON.parse(sessionStorage.getItem('userInfo')||'{}').token",timeout=15000)
            return self
        except Exception as exc:
            self.close()
            if isinstance(exc,CampusError):raise
            raise CampusError('SPACE_LOGIN_FAILED') from None
    def close(self):
        if self.sid:
            self.browsers.close(self.sid)
            self.sid=None
    def __exit__(self,*args):
        self.close()
    def call(self,path,**params):
        require(allowed_read(self.system,path,params),'UNREGISTERED_SPACE_READ')
        try:
            return self.page.evaluate(SEAT_FETCH if self.system=='seat' else ROOM_FETCH,
                                      path+('?' + urlencode(params) if params else ''))
        except Exception:
            raise CampusError('SPACE_READ_FAILED') from None
    def goto(self,path):
        require(self.system=='seat' and
                (re.fullmatch(r'/home/web/seat/area/'+NUM,path) or
                 re.fullmatch(r'/user/index/book(?:/p/'+NUM+r')?',path) or
                 path.startswith('/web/area?')),'UNREGISTERED_SPACE_PAGE')
        try:
            self.page.goto(self.base+path,wait_until='domcontentloaded',timeout=30000)
            self.browsers._settle(self.page)
        except Exception:
            raise CampusError('SPACE_PAGE_FAILED') from None

def unique(rows,key):
    require(isinstance(rows,list) and all(isinstance(x,dict) for x in rows))
    ids=[ident(x.get(key)) for x in rows]
    require(len(ids)==len(set(ids)),'DUPLICATE_SPACE_ID')
    return rows

def area_rows(raw,roots,include_inactive=False):
    require(isinstance(raw,dict) and isinstance(raw.get('seatinfo'),list))
    nodes={ident(x.get('id')):x for x in unique(raw['seatinfo'],'id')}
    def path(key,trail=()):
        require(key not in trail,'SPACE_HIERARCHY_CYCLE')
        node=nodes[key]
        parent=str(node.get('parentId',0))
        name=plain(node.get('name'))
        require(name)
        if parent not in nodes:return [name] if key in roots else None
        above=path(parent,(*trail,key))
        return above+[name] if above else [name] if key in roots else None
    rows=[]
    for key,node in nodes.items():
        chain=path(key)
        if chain is None or node.get('webhidden')==1 or not include_inactive and node.get('isValid')!=1:continue
        rows.append({'area_id':key,'name':plain(node['name']),'path':chain,
                     'parent_id':str(node.get('parentId',0)), 'leaf':node.get('type')==1,
                     'capacity':node.get('TotalCount'),'availability':None,
                     'currently_enabled':node.get('isValid')==1})
    return rows

def select(rows,params,key,name_key):
    id_value=params.get(key); name=params.get('name')
    require(not(id_value and name),'MULTIPLE_RESOURCE_SELECTORS')
    selected=[x for x in rows if x[key]==id_value] if id_value else [x for x in rows if x['name']==name or '/'.join(x.get('path',[]))==name] if name else rows
    if len(selected)==1 and (id_value or name):return selected[0],None
    return None,dict(status='NOT_FOUND' if not selected else 'NEEDS_USER_ACTION',
                     candidates=selected,reason='RESOURCE_NOT_FOUND' if not selected else 'SELECT_EXACT_RESOURCE')

def seat_catalog(reader,include_inactive=False):
    roots=reader.page.locator('a[href*="/seat/area/"]').evaluate_all(
        "(xs)=>Array.from(new Set(xs.map(x=>x.getAttribute('href').match(/\\/seat\\/area\\/(\\d+)/)?.[1]).filter(Boolean)))")
    require(roots,'SEAT_DIRECTORY_MISSING')
    reader.goto('/home/web/seat/area/'+ident(roots[0]))
    # Each root endpoint may return just its own branch. The landing page
    # can reorder the libraries; never treat its first tree as the catalog.
    catalog={}
    for key in dict.fromkeys(roots):
        if key in catalog and any(str(node.get('parentId'))==key for node in catalog.values()):continue
        raw=seat_list(reader.call('/api.php/v3areas/'+ident(key)))
        require(isinstance(raw,dict) and isinstance(raw.get('seatinfo'),list),'SEAT_DIRECTORY_BRANCH_MISSING')
        for node in unique(raw['seatinfo'],'id'):
            old=catalog.setdefault(ident(node['id']),node)
            require(all(str(old.get(k))==str(node.get(k)) for k in ('parentId','name','type','isValid','webhidden')),
                    'SPACE_DIRECTORY_CONFLICT')
    require(set(roots)<=catalog.keys(),'SEAT_DIRECTORY_BRANCH_MISSING')
    rows=area_rows({'seatinfo':list(catalog.values())},set(roots),include_inactive)
    rules=reader.page.evaluate("Array.isArray(window.ska?.bookRule)?window.ska.bookRule:[]")
    return rows,[p for x in rules if (p:=plain(x))]

def seat_slots(payload,requested):
    rows=unique(seat_list(payload),'id')
    slots=[]
    for r in rows:
        require(r.get('day')==requested,'SPACE_DATE_MISMATCH')
        a,b=clock(r.get('startTime')),clock(r.get('endTime'))
        require(a<b)
        slots.append({'segment_id':ident(r['id']),'start_time':a,'end_time':b,'platform_status':r.get('status')})
    return slots

def seat_states(payload,area):
    rows=unique(seat_list(payload),'id')
    result_rows=[]
    for r in rows:
        require(str(r.get('area'))==area,'SPACE_RESOURCE_MISMATCH')
        require(plain(r.get('name')) and type(r.get('status')) is int)
        result_rows.append({'seat_id':ident(r['id']),'name':plain(r['name']),'area_id':area,
                            'state':SEAT_STATUS.get(r['status'],'UNKNOWN'),'platform_status':r['status'],
                            'platform_label':plain(r.get('status_name')),'free':r['status']==1})
    return result_rows

def read_seat(reader,op,params):
    rows,rules=seat_catalog(reader,include_inactive=op!='seat_areas')
    if op=='seat_areas':
        if params.get('parent_id'):rows=[x for x in rows if x['parent_id']==params['parent_id']]
        if params.get('query'):rows=[x for x in rows if params['query'] in '/'.join(x['path'])]
        return result(rows,SEAT+'/home/web/f_second',rules=rules,rule_source=SEAT+'/home/web/f_second')
    chosen,problem=select(rows,params,'area_id','name')
    if problem:return result([],SEAT+'/home/web/f_second',**problem)
    area=chosen['area_id']; selected_date=day(params.get('date')).isoformat()
    if not chosen['currently_enabled']:
        return result([],SEAT+'/home/web/seat/area/'+area,status='NOT_OPEN',reason='AREA_NOT_CURRENTLY_ENABLED',resource=chosen,date=selected_date)
    reader.goto('/home/web/seat/area/'+area)
    rules=[p for x in reader.page.evaluate("Array.isArray(window.ska?.bookRule)?window.ska.bookRule:[]") if (p:=plain(x))]
    info={'resource':chosen,'date':selected_date,'rules':rules,'rule_source':SEAT+'/home/web/seat/area/'+area}
    if not chosen['leaf']:
        return result([],SEAT+'/home/web/seat/area/'+area,status='NEEDS_USER_ACTION',
                      candidates=[x for x in rows if x['parent_id']==area],reason='SELECT_LEAF_AREA',**info)
    dates=seat_list(reader.call('/api.php/space_days/'+area))
    require(isinstance(dates,list) and all(isinstance(x,dict) and isinstance(x.get('day'),str) for x in dates))
    available_dates=[day(x['day']).isoformat() for x in dates]
    info['available_dates']=available_dates
    if selected_date not in available_dates:
        return result([],info['rule_source'],status='NOT_OPEN',reason='DATE_NOT_OFFERED',**info)
    slots=seat_slots(reader.call('/api.php/space_time_buckets',area=area,day=selected_date),selected_date)
    info['offered_slots']=slots
    wanted=times(params)
    # The official map resets a custom start time to its offered slot start.
    # Only exact platform slots have been verified; never label a wider read
    # as the caller's narrower interval.
    if wanted and not any((s['start_time'],s['end_time'])==wanted for s in slots):
        return result([],info['rule_source'],status='NEEDS_USER_ACTION',reason='PLATFORM_TIME_SLOT_REQUIRED',
                      requested_start_time=wanted[0],requested_end_time=wanted[1],**info)
    matches=[s for s in slots if s['platform_status']==1 and (not wanted or (s['start_time'],s['end_time'])==wanted)]
    if not matches:
        return result([],info['rule_source'],status='NOT_OPEN',reason='TIME_NOT_OFFERED',**info)
    if len(matches)>1 and not wanted:
        return result([],info['rule_source'],status='NEEDS_USER_ACTION',reason='SELECT_TIME_RANGE',**info)
    slot=matches[0];a,b=wanted or (slot['start_time'],slot['end_time'])
    request=dict(area=area,segment=slot['segment_id'],day=selected_date,startTime=a,endTime=b)
    try:
        seats=seat_states(reader.call('/api.php/spaces_old',**request),area)
    except CampusError as exc:
        if exc.code not in ('SPACE_RESPONSE_INVALID','SPACE_READ_FAILED'):
            raise
        return result([],SEAT+'/web/area',**info,status='BROWSER_REQUIRED',complete=False,
                      start_time=a,end_time=b,warnings=['SEAT_MAP_CONTRACT_UNAVAILABLE'],
                      official_page_url=SEAT+'/web/area?'+urlencode(request))
    return result(seats,SEAT+'/web/area',**info,start_time=a,end_time=b,segment_id=slot['segment_id'],
                  free_count=sum(x['free'] for x in seats),eligibility='UNVERIFIED',
                  official_page_url=SEAT+'/web/area?'+urlencode(request))

def room_types(payload):
    rows=unique(check(payload,'room'),'kindId')
    return [{'kind_id':ident(x['kindId']),'name':plain(x.get('kindName')),'class_kind':x.get('kindClass'),
             'campus_id':x.get('campusId'),'description':plain(x.get('kindInfo')),
             'reservation_notice':plain(x.get('resvMemo')),'memo':plain(x.get('memo'))} for x in rows]

def page_window(payload,number,size):
    data=check(payload,'room')
    count=payload.get('count')
    require(isinstance(data,list) and type(count) is int and count>=0)
    require(len(data)==max(0,min(size,count-(number-1)*size)),'SPACE_PAGINATION_CHANGED')
    return data,dict(upstream_total=count,upstream_page=number,upstream_page_size=size,
                     next_page=number+1 if number*size<count else None,scope='REQUESTED_UPSTREAM_PAGE')

def minutes(t):
    h,m=map(int,clock(t).split(':'));return 60*h+m

def room_row(row,requested,party,wanted):
    rid=ident(row.get('devId'));require(plain(row.get('devName')))
    rule=row.get('resvRule'); require(rule is None or isinstance(rule,dict))
    openings=row.get('openTimes');busy=row.get('resvInfo')
    require(isinstance(openings,list) and isinstance(busy,list))
    opening_rows=[]
    for r in openings:
        a,b=clock(r.get('openStartTime')),clock(r.get('openEndTime'))
        require(a<b)
        opening_rows.append({'start_time':a,'end_time':b,'platform_limit':r.get('openLimit')})
    occupied=[]
    for r in busy:
        a,b=stamp(r.get('startTime')),stamp(r.get('endTime'))
        require(a<b)
        occupied.append({'start_time':a,'end_time':b,'platform_status':r.get('resvStatus')})
    low,high=row.get('minUser'),row.get('maxUser')
    require(type(low) is int and type(high) is int and 0<=low<=high)
    party_state='UNSPECIFIED' if party is None else 'PARTY_TOO_SMALL' if party<low else 'PARTY_TOO_LARGE' if party>high else 'IN_RANGE'
    interval_state='NOT_SELECTED'
    if wanted:
        a,b=wanted
        begin,end=requested+'T'+a,requested+'T'+b
        interval_state='FREE_TIME_ONLY'
        if not any(r['start_time']<=a<b<=r['end_time'] for r in opening_rows):
            interval_state='NOT_OPEN'
        elif any(begin<r['end_time'][:16] and end>r['start_time'][:16] for r in occupied):
            interval_state='BUSY'
        elif rule and (minutes(b)-minutes(a)<rule.get('minResvTime',0) or minutes(b)-minutes(a)>rule.get('maxResvTime',1440)):
            interval_state='DURATION_OUT_OF_RANGE'
    return {'room_id':rid,'name':plain(row['devName']),'kind_id':ident(row.get('kindId')),
            'room_name':plain(row.get('roomName')),'location':plain(row.get('labName')),
            'min_people':low,'max_people':high,'party_state':party_state,
            'platform_open_state':row.get('openState'),'platform_device_status':row.get('devStatus'),
            'opening_intervals':opening_rows,'occupied_intervals':occupied,'interval_state':interval_state,
            'eligibility':'UNVERIFIED','only_view':row.get('onlyView'),
            'rules':{k:plain(rule[k]) if isinstance(rule[k],str) else rule[k] for k in RULE_KEYS if rule and k in rule},
            'reservation_notice':plain(row.get('resvMemo'))}

def read_rooms(reader,op,params):
    types=room_types(reader.call('/ic-web/roomMenu'))
    require(all(x['name'] and type(x['class_kind']) is int for x in types))
    if op=='room_types':
        if params.get('query'):types=[x for x in types if params['query'] in x['name']]
        return result(types,ROOM+'/ic-web/roomMenu')
    chosen,problem=select(types,params,'kind_id','name')
    if problem:return result([],ROOM+'/ic-web/roomMenu',**problem)
    selected_date=day(params.get('date')).isoformat();number=params.get('page',1);size=params.get('limit',10)
    payload=reader.call('/ic-web/reserve',sysKind=chosen['class_kind'],resvDates=selected_date.replace('-',''),
                        kindIds=chosen['kind_id'],page=number,pageSize=size,labId='')
    rows,scope=page_window(payload,number,size)
    unique(rows,'devId')
    require(all(str(x.get('kindId'))==chosen['kind_id'] for x in rows),'SPACE_RESOURCE_MISMATCH')
    items=[room_row(x,selected_date,params.get('people'),times(params)) for x in rows]
    restrictions=check(reader.call('/ic-web/reserve/punishInfo'),'room')
    require(isinstance(restrictions,list))
    return result(items,ROOM+'/ic-web/reserve',**scope,resource=chosen,date=selected_date,
                  start_time=params.get('start_time'),end_time=params.get('end_time'),
                  account_restrictions={'active_records':len(restrictions),'state':'RESTRICTIONS_PRESENT' if restrictions else 'NO_REPORTED_PUNISHMENT',
                                        'eligibility':'UNVERIFIED'},rule_source=ROOM+'/ic-web/reserve')

SEAT_TABLE = """() => {
 const table=document.querySelector('table');
 return {headers:table?Array.from(table.querySelectorAll('th')).map(x=>x.textContent.trim()):[],
 rows:table?Array.from(table.querySelectorAll('tbody tr')).map(x=>({
  key:x.id,cells:Array.from(x.querySelectorAll('td')).map(x=>x.innerText.trim())})):[],
 pagination:Array.from(document.querySelectorAll('.pagination,.page')).map(x=>x.innerText).join(' '),
 links:Array.from(document.querySelectorAll('.pagination a,.page a')).map(x=>({text:x.innerText,path:new URL(x.href).pathname})),
 current:Array.from(document.querySelectorAll('.pagination .current,.page .current')).map(x=>x.textContent.trim())};
}"""

def parse_seat_reservations(data,page):
    require(isinstance(data,dict) and data.get('headers')==['预约号','预约空间','开始时间','结束时间','当前状态','预约目的','操作'],
            'SEAT_RESERVATION_TABLE_MISSING')
    require(isinstance(data.get('rows'),list) and isinstance(data.get('pagination'),str))
    total_match=re.search(r'共\s*(\d+)\s*条记录',data['pagination'])
    # Never infer an empty private list from an absent or loading table.
    require(total_match is not None,'SEAT_RESERVATION_TOTAL_MISSING')
    total=int(total_match[1]);size=20
    expected=max(0,min(size,total-(page-1)*size))
    rows=[r for r in data['rows'] if len(r.get('cells',[]))==7]
    require(len(rows)==expected,'SPACE_PAGINATION_CHANGED')
    if total>size:
        require(str(page) in data.get('current',[]),'SPACE_PAGE_MISMATCH')
    result_rows=[]
    for r in rows:
        c=r['cells'];require(re.fullmatch('list_'+NUM,r.get('key','')) is not None and all(c[:5]))
        result_rows.append({'reservation_id':r['key'][5:],'reservation_number':c[0],
                            'location':c[1],'start_time':stamp(c[2]),'end_time':stamp(c[3]),
                            'status_label':c[4],'cancel_control_visible':'取消' in c[6],
                            'cancellation_conditions':'仅记录页面是否显示取消入口；具体限制须重新核对当前官方规则。'})
    require(len({r['reservation_id'] for r in result_rows})==len(result_rows),'DUPLICATE_RESERVATION_ID')
    return result_rows,dict(upstream_total=total,upstream_page=page,upstream_page_size=size,
                           next_page=page+1 if page*size<total else None,scope='REQUESTED_UPSTREAM_PAGE_ALL_DATES_AND_STATUSES')

def own_room_row(row):
    rid=ident(row.get('resvId'))
    require(type(row.get('resvStatus')) is int and isinstance(row.get('resvDevInfoList'),list))
    rooms=[{'room_id':ident(x.get('devId')),'name':plain(x.get('devName'))} for x in row['resvDevInfoList']]
    return {'reservation_id':rid,'rooms':rooms,'start_time':stamp(row.get('resvBeginTime')),
            'end_time':stamp(row.get('resvEndTime')),'platform_status':row['resvStatus'],
            'cancellation_conditions':'须按该资源当前 resvRule.cancelTime 及平台状态重新核对；本查询不判定可以取消。'}

def read_reservations(reader,params):
    number=params.get('page',1)
    if reader.system=='seat':
        require(not(set(params)&{'start_date','end_date','status_mask','limit'}),'SEAT_HISTORY_USES_FIXED_PAGE_SIZE')
        reader.goto('/user/index/book'+('/p/'+str(number) if number>1 else ''))
        rows,scope=parse_seat_reservations(reader.page.evaluate(SEAT_TABLE),number)
        return result(rows,SEAT+'/user/index/book',**scope,system='seat',official_page_matches=True)
    start=day(params.get('start_date'));end=day(params.get('end_date',start.isoformat()))
    require(start<=end and (end-start).days<=366,'INVALID_DATE_RANGE')
    size=params.get('limit',10);mask=params.get('status_mask',6)
    payload=reader.call('/ic-web/reserve/resvInfo',beginDate=start.isoformat(),endDate=end.isoformat(),
                        needStatus=mask,page=number,pageNum=size,orderKey='gmt_create',orderModel='desc')
    rows,scope=page_window(payload,number,size)
    unique(rows,'resvId')
    return result([own_room_row(x) for x in rows],ROOM+'/ic-web/reserve/resvInfo',**scope,system='room',
                  start_date=start.isoformat(),end_date=end.isoformat(),status_mask=mask,
                  status_scope='PLATFORM_STATUS_BITMASK; default 6 = pending/effective')

def query(browsers,operation,parameters):
    system=parameters['system'] if operation=='reservations' else 'seat' if operation.startswith('seat_') else 'room'
    # Validate paired times and calendar dates before any browser/login work.
    times(parameters)
    for key in ('date','start_date','end_date'):
        if key in parameters:day(parameters[key])
    if 'start_date' in parameters or 'end_date' in parameters:
        start=day(parameters.get('start_date'));end=day(parameters.get('end_date',start.isoformat()))
        require(start<=end and (end-start).days<=366,'INVALID_DATE_RANGE')
    if parameters.get('name') and (parameters.get('area_id') or parameters.get('kind_id')):
        raise CampusError('MULTIPLE_RESOURCE_SELECTORS')
    if operation=='reservations' and system=='seat':
        require(not(set(parameters)&{'start_date','end_date','status_mask','limit'}),'SEAT_HISTORY_USES_FIXED_PAGE_SIZE')
    with Reader(browsers,system) as reader:
        if operation=='reservations':return read_reservations(reader,parameters)
        if system=='seat':return read_seat(reader,operation,parameters)
        return read_rooms(reader,operation,parameters)

