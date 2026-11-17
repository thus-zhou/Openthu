"""Read contracts for the library's public Primo, database guide and SAML loans."""
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

from .bridge import CampusError

DISCOVER = 'https://discover.lib.tsinghua.edu.cn'
PRIMO = DISCOVER + '/tsinghuasearch/primo/'
GUIDE = 'https://ecollection.lib.tsinghua.edu.cn'
GUIDE_API = GUIDE + '/databaseguide/guide/'
DATABASES = GUIDE + '/databasenav/'
OFF_CAMPUS = 'https://lib.tsinghua.edu.cn/tjfw/xwfw.htm'
LOANS = DISCOVER + '/entrance/myLibEntrance/myLibIndex/MyLibBorrowNow'
VERSION = 'library-read-v1'
SCOPES = {'all': 'default_scope', 'print': 'print_scope', 'online': 'online_scope'}
FIELDS = {'keyword': 'any', 'title': 'title', 'author': 'creator'}

def require(condition):
    if not condition:
        raise CampusError('LIBRARY_RESPONSE_INVALID')

def code_check(data):
    require(isinstance(data, dict))
    code = data.get('code')
    message = str(data.get('message') or data.get('msg') or '')
    if code in (401, 403):
        raise CampusError('AUTH_REQUIRED' if code == 401 else 'FORBIDDEN')
    if code == 429:
        raise CampusError('RATE_LIMITED')
    if code == 503 or re.search('系统维护|维护中|系统升级|暂停服务', message):
        raise CampusError('MAINTENANCE')
    if code != 200:
        raise CampusError('LIBRARY_QUERY_FAILED')
    return data.get('data')

class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('style', 'script'):
            self.hidden += 1
        if tag in ('p', 'br', 'li', 'div'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('style', 'script') and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

def plain(value):
    if not isinstance(value, str):
        return None
    parser = PlainText()
    parser.feed(value)
    return ' '.join(''.join(parser.parts).split()) or None

def values(value):
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [s for item in value if (s := plain(item))]

def first(value):
    return next(iter(values(value)), None)

def safe_link(value):
    if not isinstance(value, str) or re.search(r'[\s\\\x00-\x1f]', value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.port not in (None, 80, 443):
            return None
        if url.fragment or any(re.search(r'token|ticket|session|auth|nonce|saml|sign|password|apikey|ctx_id', k, re.I) for k, _ in parse_qsl(url.query)):
            return None
    except ValueError:
        return None
    return value

def retry_after_ms(response):
    value = response.headers.get('Retry-After')
    if isinstance(value, str) and value.strip().isdigit():
        return min(int(value.strip()), 86400) * 1000
    return None

ENDPOINTS = {
    'jwt': ('POST', PRIMO+'jwt'), 'search': ('POST', PRIMO+'listData'),
    'detail': ('POST', PRIMO+'pnxDetail'), 'holdings': ('POST', PRIMO+'holdings'),
    'copies': ('POST', PRIMO+'holdingItems'), 'db_search': ('GET', GUIDE_API+'searchDesc'),
    'db_detail': ('POST', GUIDE_API+'viewDatabaseDesc'),
}

class PublicReader:
    """A fresh guest session. The guest header is scoped to Primo only."""
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/142.0.0.0 Safari/537.36'
        self.jwt = None

    def call(self, operation, **params):
        method, url = ENDPOINTS[operation]
        headers = {'Referer': DISCOVER+'/' if url.startswith(PRIMO) else DATABASES}
        if url.startswith(PRIMO) and operation != 'jwt':
            if not self.jwt:
                token = code_check(self.call('jwt'))
                require(isinstance(token, str) and re.fullmatch(r'"?[\w.-]+"?', token, re.ASCII))
                self.jwt = token.strip('"')
            headers['PrimoAuthorization'] = 'Bearer '+self.jwt
        try:
            with self.session.request(method, url, headers=headers, params=params if method=='GET' else None,
                                      data=params if method=='POST' else None, timeout=(6, 20),
                                      allow_redirects=False, stream=True) as response:
                if response.status_code in (401, 403, 429, 503):
                    raise CampusError({401:'AUTH_REQUIRED', 403:'FORBIDDEN', 429:'RATE_LIMITED',
                                       503:'MAINTENANCE'}[response.status_code],
                                      retry_after_ms(response) if response.status_code == 429 else None)
                if 300 <= response.status_code < 400:
                    raise CampusError('LIBRARY_REDIRECT_BLOCKED')
                require(response.status_code == 200)
                body = bytearray()
                for block in response.iter_content(65536):
                    body.extend(block)
                    if len(body) > 8*1024*1024:
                        raise CampusError('LIBRARY_RESPONSE_TOO_LARGE')
                return json.loads(body)
        except requests.RequestException:
            raise CampusError('LIBRARY_NETWORK_ERROR') from None
        except (ValueError, UnicodeError):
            raise CampusError('LIBRARY_RESPONSE_INVALID') from None

def record(doc):
    pnx = doc.get('pnx', {})
    control, display, delivery = pnx.get('control', {}), pnx.get('display', {}), doc.get('delivery', {})
    rid, title = first(control.get('recordid')), first(display.get('title'))
    require(rid and title and re.fullmatch(r'[\w.:-]{1,250}', rid, re.ASCII))
    add = pnx.get('addata', {})
    electronic = []
    for field in ('linktorsrc', 'linktohtml', 'linktopdf'):
        for raw in values(pnx.get('links', {}).get(field)):
            match = re.search(r'\$\$U([^$]+)', raw)
            if match and (link := safe_link(match[1])) and link not in electronic:
                electronic.append(link)
    detail_url = DISCOVER+'/entrance/searchEntrance/resourceDetail?'+urlencode({'id':rid,'context':doc.get('context','L'),'adaptor':doc.get('adaptor','Local Search Engine')})
    categories = values(delivery.get('deliveryCategory'))
    return {'record_id':rid, 'title':title, 'authors':values(display.get('creator') or add.get('au') or add.get('aucorp')),
            'edition':first(display.get('edition') or add.get('edition')), 'publication_date':first(display.get('creationdate') or add.get('date')),
            'publisher':first(display.get('publisher')), 'isbn':values(add.get('isbn')), 'resource_type':values(display.get('type')),
            'delivery_categories':categories, 'availability':values(delivery.get('availability')),
            'availability_scope':'SOURCE_LISTING_NOT_HOLDINGS',
            'electronic_links':electronic, 'electronic_access_page':detail_url if electronic or 'Alma-E' in categories else None,
            'detail_url':detail_url}

def result(items, url, **extra):
    return {'items':items, 'url':url, 'complete':True, 'warnings':[], **extra}

def window(items, total, offset, limit, url):
    require(type(total) is int and total >= 0)
    expected = min(limit, max(0, total-offset))
    complete = len(items) == expected
    return result(items, url, complete=complete, warnings=[] if complete else ['UPSTREAM_WINDOW_COUNT_MISMATCH'],
                  upstream_total=total, offset=offset, limit=limit,
                  next_offset=offset+len(items) if items and offset+len(items)<total else None,
                  scope='requested upstream window')

def catalog_search(call, p):
    offset, limit = p.get('offset', 0), p.get('limit', 20)
    term = p['query'].strip()
    if not term or re.search(r'[\x00-\x1f]|\|,\|', term):
        raise CampusError('INVALID_LIBRARY_QUERY')
    raw = code_check(call('search', search=f"{FIELDS[p.get('field','keyword')]},contains,{term}", sort='rank',
                          scope=SCOPES[p.get('scope','all')], offset=offset, pageSize=limit))
    require(isinstance(raw, dict) and isinstance(raw.get('docs'), list) and isinstance(raw.get('info'), dict))
    rows = [record(doc) for doc in raw['docs']]
    out = window(rows, raw['info'].get('total'), offset, limit, PRIMO+'listData')
    if rows and str(raw['info'].get('first')) != str(offset+1):
        out.update(complete=False, warnings=['UPSTREAM_OFFSET_MISMATCH'], next_offset=None)
    if len({r['record_id'] for r in rows}) != len(rows):
        out.update(complete=False, warnings=out['warnings']+['DUPLICATE_RECORD_ID'])
    return out

def identifier(value):
    require(isinstance(value,str) and re.fullmatch(r'[0-9]{1,30}', value))
    return value

def described(obj, field):
    v = obj.get(field) or {}
    require(isinstance(v,dict))
    return plain(v.get('desc')) or plain(v.get('value'))

def read_copies(call, mmsid, h, budget):
    holding_id = identifier(h.get('holding_id'))
    item = {'mms_id':mmsid,'holding_id':holding_id,'library':described(h,'library'),
            'location':described(h,'location'),'call_number':plain(h.get('call_number')), 'copies':[]}
    offset = 0
    seen = set()
    while True:
        if budget[0] <= 0:
            return item, 'HOLDINGS_READ_LIMIT'
        budget[0] -= 1
        try:
            data = code_check(call('copies',mmsid=mmsid,holdingid=holding_id,userid='',lang='zh',offset=offset))
        except CampusError as exc:
            return item, exc.code
        require(isinstance(data,dict))
        total, batch = data.get('total_record_count'), data.get('item',[])
        require(type(total) is int and total>=0 and isinstance(batch,list))
        for row in batch:
            require(row.get('holding_data',{}).get('holding_id')==holding_id and row.get('bib_data',{}).get('mms_id')==mmsid)
            copy = row['item_data']
            pid = identifier(copy.get('pid'))
            if pid in seen:
                return item, 'REPEATED_COPY'
            seen.add(pid)
            process = (copy.get('process_type') or {}).get('value')
            policy = described(copy,'policy')
            unavailable = process in {'LOAN','LOST_LOAN','HOLDSHELF','TRANSIT','MISSING','ILL','ACQ','WORK_ORDER_DEPARTMENT'} or bool(policy and re.search('不外借|不可借|仅限馆内|非外借',policy))
            item['copies'].append({'item_id':pid, 'status':described(copy,'work_order_type') or described(copy,'process_type') or described(copy,'base_status'),
                                   'loan_policy':policy,'borrowable':False if unavailable else None,
                                   'borrowable_state':'NOT_BORROWABLE' if unavailable else 'UNVERIFIED',
                                   'availability_scope':'COPY_STATUS_NOT_ACCOUNT_ELIGIBILITY',
                                   'borrow_eligibility':'UNVERIFIED','due':plain(copy.get('due_date'))})
        offset += len(batch)
        if offset == total:
            return item, None
        if not batch or offset > total:
            return item, 'COPY_COUNT_MISMATCH'

def catalog_detail(call, p):
    rid = p['record_id']
    raw = code_check(call('detail',recordid=rid,lang='zh'))
    require(isinstance(raw,dict))
    book = record(raw)
    require(book['record_id']==rid)
    book['holdings'] = []
    out = result([book], PRIMO+'pnxDetail')
    physical = 'Alma-P' in book['delivery_categories'] or bool(raw['delivery'].get('holding'))
    if not physical:
        book['holdings_state'] = 'NO_PHYSICAL_INDICATION'
        return out
    ids = values(raw['pnx'].get('search',{}).get('addsrcrecordid'))
    budget = [32]
    warnings = []
    if not ids:
        warnings.append('PHYSICAL_RECORD_ID_MISSING')
    for mid in dict.fromkeys(ids):
        try:
            if budget[0]<=0:
                warnings.append('HOLDINGS_READ_LIMIT')
                break
            budget[0]-=1
            data = code_check(call('holdings',mmsid=identifier(mid),lang='zh'))
            require(isinstance(data,dict))
            entries, total = data.get('holding',[]), data.get('total_record_count')
            require(isinstance(entries,list) and type(total) is int and len(entries)==total)
            bib = data.get('bib_data') or {}
            if bib.get('mms_id'):
                require(bib['mms_id']==mid)
            book['edition'] = book['edition'] or plain(bib.get('complete_edition'))
            for h in entries:
                if str(h.get('suppress_from_publishing')).lower()=='true':
                    continue
                holding, problem = read_copies(call, mid, h, budget)
                book['holdings'].append(holding)
                if problem:
                    warnings.append(problem)
        except (CampusError,KeyError,TypeError,AttributeError) as exc:
            warnings.append(getattr(exc,'code','LIBRARY_RESPONSE_INVALID'))
    book['holdings_state'] = 'PARTIAL' if warnings else 'READ'
    out.update(complete=not warnings,warnings=list(dict.fromkeys(warnings)))
    return out

def database_links(call,p):
    if p.get('database_id'):
        mid = p['database_id']
        data = code_check(call('db_detail',mmsid=mid))
        require(isinstance(data,dict) and data.get('mmsid')==mid and plain(data.get('databasename')))
        return result([{'database_id':mid,'name':plain(data['databasename']), 'official_url':DATABASES+'entrance/detail?'+urlencode({'mmsid':mid}),
                        'offcampus_conditions':plain(data.get('offcampus')), 'special_notice':plain(data.get('specialNotice')),
                        'access_state':'OFFICIAL_GUIDE_ONLY',
                        'electronic_access':{'state':'OFFICIAL_GUIDE_ONLY','institutional_authorization':'UNVERIFIED',
                                             'off_campus_access':'UNVERIFIED','third_party_terms':'NOT_CHECKED',
                                             'cookie_forwarded':False}}],GUIDE_API+'viewDatabaseDesc')
    if not p.get('query'):
        return result([{'name':'数据库导航','official_url':DATABASES},{'name':'校外访问说明','official_url':OFF_CAMPUS}],
                      'https://lib.tsinghua.edu.cn/',scope='registered official entry points',level='DIRECTORY_CACHE',
                      source_reviewed_on='2026-11-03')
    if not p['query'].strip():
        raise CampusError('INVALID_LIBRARY_QUERY')
    offset, limit = p.get('offset',0), p.get('limit',20)
    raw = call('db_search',searchKey=p['query'],offset=offset,pageSize=limit)
    rows = code_check(raw)
    require(isinstance(rows,list))
    entries = []
    for row in rows:
        mid = identifier(row.get('mmsid'))
        require(plain(row.get('databasename')))
        entries.append({'database_id':mid,'name':plain(row['databasename']), 'official_url':DATABASES+'entrance/detail?'+urlencode({'mmsid':mid}),
                        'source_updated_at':plain(row.get('updateTime')), 'access_state':'OFFICIAL_GUIDE_ONLY',
                        'electronic_access':{'state':'OFFICIAL_GUIDE_ONLY','institutional_authorization':'UNVERIFIED',
                                             'off_campus_access':'UNVERIFIED','third_party_terms':'NOT_CHECKED',
                                             'cookie_forwarded':False}})
    return window(entries,raw.get('total'),offset,limit,GUIDE_API+'searchDesc')

def loan_date(raw):
    v = first(raw)
    if not v:
        return None
    try:
        return datetime.strptime(v,'%Y%m%d').date().isoformat() if re.fullmatch(r'\d{8}',v) else date.fromisoformat(v).isoformat()
    except ValueError:
        raise CampusError('LIBRARY_LOAN_DATE_INVALID') from None

def parse_loans(payload):
    data = code_check(payload)
    loans = data.get('data',{}).get('loans') if isinstance(data,dict) else None
    require(isinstance(loans,dict) and isinstance(loans.get('loan'),list))
    more = first(loans.get('showmore'))
    require(more in ('Y','N'))
    records = []
    for row in loans['loan']:
        title = first(row.get('title'))
        require(title and first(row.get('loanid')))
        due = loan_date(row.get('duedate'))
        hour = first(row.get('duehour'))
        if hour and not re.fullmatch(r'(?:[01]\d|2[0-3])[0-5]\d',hour):
            raise CampusError('LIBRARY_LOAN_TIME_INVALID')
        renew_raw = first(row.get('renew'))
        records.append({'loan_id':first(row['loanid']), 'title':title, 'loan_date':loan_date(row.get('loandate')),
                        'due_date':due,'due_time':hour[:2]+':'+hour[2:] if hour else None,
                        'due_at':(due+'T'+hour[:2]+':'+hour[2:]+'+08:00' if due and hour else due),
                        'due_precision':('minute' if hour else 'date') if due else None,
                        'timezone':'Asia/Shanghai','overdue':overdue(due,hour),
                        'renewable':{'Y':True,'N':False}.get(renew_raw),
                        'renew_flag':renew_raw if renew_raw in ('Y','N') else None,
                        'renewal_state':{'Y':'ELIGIBLE','N':'NOT_ELIGIBLE'}.get(renew_raw,'UNKNOWN'),
                        'renewal':{'state':'HINT_ONLY','source_field':'renew',
                                   'does_not_change_due_date':True,'execution_attempted':False},
                        'renewal_hints':values((row.get('renewstatuses') or {}).get('renewstatus'))})
    return records, more=='Y'

def overdue(due,hour):
    if not due:
        return None
    now = datetime.now(timezone(timedelta(hours=8)))
    if hour:
        return now.strftime('%Y-%m-%d%H%M') > due+hour
    return now.date().isoformat() > due

# Only the official site's active-loan read uses this authenticated context.
LOAN_FETCH = """async offset => {
 if (location.origin !== 'https://discover.lib.tsinghua.edu.cn' ||
     !location.pathname.toLowerCase().endsWith('/mylibborrownow') ||
     !JSON.parse(localStorage.getItem('userInfo') || 'null')?.accountid)
   return {code:401};
 const response = await fetch('/tsinghuasearch/primo/loans', {method:'POST',
   redirect:'error', credentials:'same-origin', signal:AbortSignal.timeout(20000),
   headers:{PrimoAuthorization:'Bearer '+localStorage.getItem('primoExploreJwt')},
   body:new URLSearchParams({lang:'zh_CN',offset:String(offset),bulk:'10',type:'active'})});
 return response.ok ? response.json() : {code:response.status};
}"""

def read_loans(browsers):
    pagination = {'kind':'OWN_LOANS','page_size':10,'upstream_total':None,
                  'scope':'ACTIVE_LOANS_PAGED_10_PER_REQUEST','complete':False}
    opened = browsers.open(service_id='library-catalog',visible=False)
    sid = opened['session_id']
    try:
        if opened['status']=='TWO_FACTOR_REQUIRED':
            raise CampusError('TWO_FACTOR_REQUIRED')
        page = browsers.sessions[sid]['page']
        parsed = urlsplit(page.url)
        if parsed.hostname!='discover.lib.tsinghua.edu.cn' or parsed.path.lower()!=urlsplit(LOANS).path.lower():
            raise CampusError('AUTH_REQUIRED')
        items, seen = [], set()
        for offset in range(1,1002,10):
            try:
                rows, more = parse_loans(page.evaluate(LOAN_FETCH,offset))
            except Exception as exc:
                if not items:
                    if isinstance(exc, CampusError):
                        raise
                    raise CampusError('LIBRARY_LOAN_PAGE_FAILED') from None
                return result(items,PRIMO+'loans',complete=False,warnings=['LATER_LOAN_PAGE_FAILED'],
                              pagination={**pagination,'pages_read':(offset-1)//10+1})
            if offset==1:
                body = page.locator('.el-table__body-wrapper')
                observed = body.inner_text() if body.count()==1 else ''
                marker = page.locator('.el-table__empty-text')
                empty = any(t.strip() in ('暂无数据','No Data','No data') for t in marker.all_text_contents())
                matches = all(row['title'] in observed for row in rows) if rows else empty
                if not matches:
                    return result(rows,PRIMO+'loans',complete=False,warnings=['LOAN_PAGE_DISAGREES_WITH_API'],official_page_matches=False,
                                  pagination={**pagination,'pages_read':1})
            if any(row['loan_id'] in seen for row in rows) or len({row['loan_id'] for row in rows})!=len(rows):
                return result(items,PRIMO+'loans',complete=False,warnings=['REPEATED_LOAN_PAGE'],
                              pagination={**pagination,'pages_read':(offset-1)//10+1})
            items.extend(rows)
            seen.update(row['loan_id'] for row in rows)
            if not more:
                return result(items,PRIMO+'loans',official_page_matches=True,
                              pagination={**pagination,'complete':True,'pages_read':(offset-1)//10+1})
            if len(rows)!=10:
                break
        return result(items,PRIMO+'loans',complete=False,warnings=['LOAN_PAGINATION_UNVERIFIED'],
                      pagination={**pagination,'pages_read':max(1,(offset-1)//10+1)})
    finally:
        browsers.close(sid)

def query(browsers, operation, parameters):
    if operation=='loans':
        return read_loans(browsers)
    reader = PublicReader()
    try:
        handler = {'catalog_search':catalog_search,'catalog_detail':catalog_detail,'database_links':database_links}[operation]
        return handler(reader.call,parameters)
    finally:
        reader.session.close()

