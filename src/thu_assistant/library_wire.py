"""Narrow, account-bound reads and a single-use library mutation transport."""
from hashlib import sha256
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit
from . import spaces as s
from .actions import NotDispatched, RequestSpec
from .bridge import CampusError

# Parse additional pages inside this authenticated library context. Only the
# columns already used by the read adapter leave the browser.
_HISTORY = """async ({url,page}) => {
 const u=new URL(url);
 if(u.origin!==location.origin)throw Error('ORIGIN');
 const r=await fetch(u,{redirect:'error',signal:AbortSignal.timeout(18000)});
 if(!r.ok)throw Error('PAGE');
 const doc=new DOMParser().parseFromString(await r.text(),'text/html');
 const table=doc.querySelector('table');
 return {headers:table?Array.from(table.querySelectorAll('th'),x=>x.textContent.trim()):[],
 rows:table?Array.from(table.querySelectorAll('tbody tr'),row=>({key:row.id,cells:Array.from(row.querySelectorAll('td'),x=>x.textContent.trim())})):[],
 pagination:Array.from(doc.querySelectorAll('.pagination,.page'),x=>x.textContent).join(' '),
 current:Array.from(doc.querySelectorAll('.pagination .current,.page .current'),x=>x.textContent.trim())};
}"""


def seat_history(reader):
    rows = []
    ids = set()
    total = None
    for page in range(1,101):
        suffix = '/user/index/book' + (f'/p/{page}' if page > 1 else '')
        raw = reader.page.evaluate(_HISTORY,{'url':reader.base+suffix,'page':page})
        block, scope = s.parse_seat_reservations(raw,page)
        current = {x['reservation_id'] for x in block}
        s.require(total in (None,scope['upstream_total']) and not ids.intersection(current),'LIBRARY_HISTORY_CHANGED')
        total = scope['upstream_total']
        ids.update(current)
        rows.extend(block)
        if not scope['next_page']:
            s.require(len(rows)==total,'LIBRARY_HISTORY_INCOMPLETE')
            return rows
    raise CampusError('LIBRARY_HISTORY_LIMIT')


def room_history(reader, day):
    rows = []
    ids = set()
    total = None
    for page in range(1,51):
        payload = reader.call('/ic-web/reserve/resvInfo',beginDate=day,endDate=day,needStatus=65535,
                              page=page,pageNum=50,orderKey='gmt_create',orderModel='desc')
        block,scope = s.page_window(payload,page,50)
        s.unique(block,'resvId')
        current = {s.ident(x.get('resvId')) for x in block}
        s.require(total in (None,scope['upstream_total']) and not ids.intersection(current),'LIBRARY_HISTORY_CHANGED')
        total=scope['upstream_total']
        ids.update(current)
        rows.extend(block)
        if not scope['next_page']:
            s.require(len(rows)==total,'LIBRARY_HISTORY_INCOMPLETE')
            return rows
    raise CampusError('LIBRARY_HISTORY_LIMIT')


def seat_map(reader, availability, seat_id):
    u = urlsplit(availability['official_page_url'])
    reader.goto(u.path+'?'+u.query)
    try:
        value = reader.page.evaluate("""id=>{
          const rows=Array.from(document.querySelectorAll('.floor-area .seat'),e=>{
            const x=JSON.parse(e.getAttribute('data-data'));
            return {id:String(x.id),control:e.getAttribute('data-no'),area:String(x.area),name:x.name,status:x.status};
          });
          return {seat:rows.filter(x=>x.id===id),sameName:rows.filter(x=>x.name===rows.find(y=>y.id===id)?.name).length,
            date:ska.day,start:ska.startTime,end:ska.endTime,segment:String(ska.segment),area:String(ska.area),
            authenticated:!!(ska.access_token&&ska.userid),restricted:!!jQuery('.renege:visible').length};
        }""",seat_id)
    except Exception:
        raise CampusError('SEAT_MAP_UNAVAILABLE') from None
    return value


def room_identity(reader):
    return reader.page.evaluate("""()=>{
      const u=JSON.parse(sessionStorage.getItem('userInfo')||'{}');
      const cfg=document.querySelector('#app')?.__vue__?.$store?.state?.config;
      return {accNo:String(u.accNo||''),login:u.logonName,disabled:u.status===2||u.localstatus===2,
              verification:cfg?.resvCode??null,form:cfg?{notice:!!cfg.needToKnow,
                memo:cfg.resvmemo??null,theme:cfg.resvTheme??null,freeTitle:cfg.resvthemefix??null,
                cancellationReduction:cfg.cancelTimeReduceMinutes??null}:null};
    }""")


def member_matches(reader, login):
    # Fixed lookup, never return unrelated names or identity details.
    return reader.page.evaluate("""async key=>{
      const user=JSON.parse(sessionStorage.getItem('userInfo')||'null');
      if(location.origin!=='https://cab.lib.tsinghua.edu.cn'||!user?.token)throw Error('AUTH');
      const q=new URLSearchParams({key,page:1,pageNum:10});
      const r=await fetch('/ic-web/account/getMembers?'+q,{headers:{token:user.token,lan:'1'},redirect:'error',
          signal:AbortSignal.timeout(18000)});
      if(!r.ok)throw Error('MEMBERS');
      const d=await r.json();
      if(d.code!==0||!Array.isArray(d.data))throw Error('MEMBERS');
      return {complete:d.data.length<10 && !(Number(d.count)>d.data.length),
              exact:d.data.filter(x=>key===user.logonName ? String(x.accNo)===String(user.accNo) : x.logonName===key)
                          .map(x=>({accNo:String(x.accNo||''),disabled:x.status===2||x.localstatus===2}))};
    }""",login)


def specification(system, path, body):
    origin = s.SEAT if system=='seat' else s.ROOM
    if system=='seat':
        s.require(re.fullmatch(r'/api.php/(spaces/[1-9]\d*/book|profile/books/[1-9]\d*)',path),'LIBRARY_REQUEST_NOT_REGISTERED')
        return RequestSpec(origin+path,'POST',urlencode(body),encoding='form')
    s.require(path in ('/ic-web/reserve','/ic-web/reserve/delete'),'LIBRARY_REQUEST_NOT_REGISTERED')
    return RequestSpec(origin+path,'POST',json.dumps(body,ensure_ascii=False,separators=(',',':')))


_PREPARE = """async ({system,url,body}) => {
 const u=new URL(url);
 if(u.origin!==location.origin)throw Error('ORIGIN');
 const headers={},options={method:'POST',redirect:'error',credentials:'same-origin',headers};
 if(system==='seat'){
   if(!window.ska?.access_token||!window.ska?.userid)throw Error('AUTH');
   options.body=new URLSearchParams({...body,access_token:ska.access_token,userid:ska.userid}).toString();
   headers['Content-Type']='application/x-www-form-urlencoded; charset=UTF-8';
 }else{
   const user=JSON.parse(sessionStorage.getItem('userInfo')||'{}');
   if(!user.token)throw Error('AUTH');
   Object.assign(headers,{token:user.token,lan:'1','Content-Type':'application/json'});
   options.body=JSON.stringify(body);
 }
 window.__thuLibrarySingle={url,options};
 const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(options.body));
 return Array.from(new Uint8Array(digest),x=>x.toString(16).padStart(2,'0')).join('');
}"""
_SEND = """async system=>{
 const p=window.__thuLibrarySingle;delete window.__thuLibrarySingle;
 const r=await fetch(p.url,{...p.options,signal:AbortSignal.timeout(18000)});
 if(!r.ok)return false;
 const d=await r.json();
 return system==='seat'?d.status===1:d.code===0;
}"""


def dispatch_exact(reader, system, path, body, gate):
    spec = specification(system,path,body)
    url = (reader.base if system=='seat' else s.ROOM)+path
    checksum = reader.page.evaluate(_PREPARE,{'system':system,'url':url,'body':body})
    pattern = re.compile('^'+re.escape(url)+'$')
    def route_once(route):
        req = route.request
        if req.frame!=reader.page.main_frame or req.url!=url or req.method!='POST':
            return route.abort()
        transmitted=req.post_data or ''
        if sha256(transmitted.encode()).hexdigest()!=checksum:
            return route.abort()
        if system=='seat':
            pairs=parse_qsl(transmitted,keep_blank_values=True)
            auth=[k for k,v in pairs if k in ('userid','access_token') and v]
            if sorted(auth)!=['access_token','userid']:return route.abort()
            transmitted=urlencode([(k,v) for k,v in pairs if k not in ('userid','access_token')])
        if gate.consume(spec.url,req.method,transmitted):
            return route.continue_()
        return route.abort()
    reader.page.route(pattern,route_once)
    try:
        accepted=reader.page.evaluate(_SEND,system)
    finally:
        reader.page.unroute(pattern,route_once)
    if gate.sent!=1:raise NotDispatched()
    return accepted is True

