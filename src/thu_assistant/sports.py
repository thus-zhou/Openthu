"""Typed sports reads over the official PC frontend, with explicit upstream windows."""
from datetime import date, datetime, time, timedelta, timezone
import html
import re

from .bridge import CampusError
from .sports_transport import Reader, SCENE, LOCATION, PAGE, RECORDS, DETAIL, HOME, PERSONAL

VERSION = 'sports-v1'
TZ = timezone(timedelta(hours=8))
MODES = ('SPORT_GROUP', 'SPORT_PERSON')
PAYMENT = {1:'PENDING_PAYMENT', 2:'PROCESSING', 4:'PAID', 8:'PAYMENT_CANCELLED', 16:'PAYMENT_EXPIRED'}
REFUND = {0:'NONE', 1:'REQUESTED', 2:'REJECTED', 4:'REFUNDING', 8:'REFUNDED', 16:'FAILED', 128:'PENDING', 256:'MANUAL'}
ID = {'type':'string','pattern':r'[A-Za-z0-9_-]{1,100}'}
DAY = {'type':'string','pattern':r'\d{4}-\d{2}-\d{2}'}
WINDOW = {'page':{'type':'integer','minimum':1,'maximum':1000}, 'limit':{'type':'integer','minimum':1,'maximum':50}}


def require(condition, code='SPORTS_RESPONSE_INVALID'):
    if not condition:
        raise CampusError(code)


def ident(value):
    require(isinstance(value, (str,int)) and not isinstance(value, bool) and re.fullmatch(ID['pattern'], str(value)))
    return str(value)


def label(value):
    return html.unescape(re.sub('<[^>]+>', '', str(value or ''))).strip()[:500]


def local_day(value):
    try:
        return date.fromisoformat(value) if value else datetime.now(TZ).date()
    except (ValueError, TypeError):
        raise CampusError('INVALID_DATE_RANGE') from None


def stamp(value):
    if not value:
        return None
    try:
        if type(value) is int:
            dt = datetime.fromtimestamp(value / 1000, TZ)
        else:
            dt = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).astimezone(TZ).isoformat(timespec='seconds')
    except (ValueError, OverflowError, OSError):
        raise CampusError('SPORTS_RESPONSE_INVALID') from None


def array(raw):
    rows = raw.get('data')
    require(isinstance(rows, list) and all(isinstance(x, dict) for x in rows))
    return rows


def page_rows(raw, page, size, key):
    rows = array(raw)
    total = raw.get('count')
    require(type(total) is int and total >= 0 and len(rows) == max(0, min(size,total-(page-1)*size)),
            'SPORTS_PAGINATION_CHANGED')
    ids = [ident(x.get(key)) for x in rows]
    require(len(set(ids)) == len(ids), 'SPORTS_DUPLICATE_RECORD')
    return rows, {'upstream_page':page,'upstream_page_size':size,'upstream_total':total,
                  'next_page':page+1 if page*size < total else None}


def result(items, url, **extra):
    return {'items':items,'url':url,'complete':True,'warnings':[], 'timezone':'Asia/Shanghai',
            'freshness':'LIVE_AT_CHECKED_AT', **extra}


def scenes(reader, query=''):
    rows = array(reader.call(SCENE))
    items = []
    seen = set()
    for row in rows:
        sid = ident(row.get('uuid'))
        require(sid not in seen and row.get('sceneName'), 'SPORTS_SCENE_CATALOG_CHANGED')
        seen.add(sid)
        name = label(row['sceneName'])
        if query and query.replace('兵乓','乒乓') not in name.replace('兵乓','乒乓'):
            continue
        items.append({'scene_id':sid,'name':name,'site_type':row.get('relatedType'),
                      'mode':row.get('sceneUseType') if row.get('sceneUseType') in MODES else None})
    return result(items, HOME, complete=bool(rows), warnings=[] if rows else ['SCENE_CATALOG_EMPTY_UNVERIFIED'],
                  scope='OFFICIAL_SCENE_DIRECTORY', catalog_count=len(rows))


def rooms(reader, scene_id):
    nodes = ['']
    requests = 0
    for kind in ('CAMPUS','BUILDING','FLOOR','ROOM'):
        children = {}
        for parent in nodes:
            if requests >= 60:
                raise CampusError('SPORTS_LOCATION_TREE_LIMIT')
            requests += 1
            raw = reader.call(LOCATION, {'sceneUuid':scene_id,'siteType':kind,'siteUuid':parent})
            for row in array(raw):
                rid = ident(row.get('uuid'))
                require(row.get('siteType') in (kind,None), 'SPORTS_LOCATION_TYPE_CHANGED')
                children[rid] = {'room_id':rid,'name':label(row.get('siteName') or row.get('propName')),
                                 'location':label(row.get('location'))}
        nodes = list(children)
        if not nodes:
            return []
    return list(children.values())


def slot_start(raw, clock, day):
    require(isinstance(clock,str) and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?',clock))
    if re.fullmatch(r'\d{8}', str(raw)):
        try:
            declared = datetime.strptime(str(raw),'%Y%m%d').date().isoformat()
        except ValueError:
            raise CampusError('SPORTS_SESSION_DATE_MISMATCH') from None
    else:
        declared = stamp(raw)
    require(declared is not None, 'SPORTS_SESSION_DATE_MISSING')
    return stamp(declared[:10]+'T'+clock)


def field_row(row, scene_id, mode, day, room_id):
    require(ident(row.get('sceneUuid')) == scene_id, 'SPORTS_SCENE_MISMATCH')
    slots = row.get('sessionVo')
    require(slots is None or isinstance(slots,list))
    sessions = []
    for entry in slots or []:
        require(isinstance(entry,dict) and entry.get('sceneUseType') == mode, 'SPORTS_MODE_MISMATCH')
        start = slot_start(entry.get('beginDate'),entry.get('beginTime'),day)
        end = slot_start(entry.get('endDate'),entry.get('endTime'),day)
        require(start[:10] == day and end > start, 'SPORTS_SESSION_DATE_MISMATCH')
        status = entry.get('reserveStatus') or {}
        fee = entry.get('userFeeDetails') or {}
        cents = fee.get('chargingUnitPrice')
        require(cents is None or type(cents) is int and cents >= 0, 'SPORTS_FEE_FORMAT_CHANGED')
        total = fee.get('payableAmount')
        require(total is None or type(total) is int and total >= 0, 'SPORTS_FEE_FORMAT_CHANGED')
        available = {'Y':True,'N':False}.get(status.get('reserveStatus'))
        sessions.append({'session_id':ident(entry.get('uuid')), 'start_at':start, 'end_at':end,
                         'available':available, 'reason':label(status.get('reserveStatusReason')),
                         'unit_fee_fen':cents, 'quoted_total_fen':total, 'currency':'CNY','charging_mode':fee.get('chargingMode'),
                         'payment_hint':fee.get('payType'),'capacity':entry.get('allowUserNum'),
                         'reserved_count':entry.get('resvUserNum')})
    require(len({s['session_id'] for s in sessions}) == len(sessions), 'SPORTS_DUPLICATE_SESSION')
    rule = row.get('reserveRule') or {}
    form = row.get('formRuleVo') or {}
    return {'site_id':ident(row.get('uuid')), 'name':label(row.get('siteName')), 'site_type':row.get('siteType'),
            'scene_id':scene_id,'room_id':room_id,'mode':mode,'sessions':sessions,
            'free_period_requires_official_page':not sessions and row.get('supportPeriod') == 'Y',
            'cancel_rule':{k:rule.get(k) for k in ('cancelTime','cancelBeforeStart','deadlineTime')},
            'form_id':form.get('formUuid'), 'form_state_known':'formRuleVo' in row}


def availability(reader, params):
    day = local_day(params.get('date')).isoformat()
    catalog = scenes(reader, params.get('name',''))
    candidates = [x for x in catalog['items'] if not params.get('scene_id') or x['scene_id'] == params['scene_id']]
    if len(candidates) != 1:
        return result([], HOME, complete=False,status='NEEDS_USER_ACTION', candidates=candidates,
                      reason='SELECT_EXACT_SCENE' if candidates else 'SCENE_NOT_FOUND')
    scene = candidates[0]
    location = rooms(reader, scene['scene_id'])
    chosen = [x for x in location if not params.get('room_id') or x['room_id'] == params['room_id']]
    if len(chosen) != 1:
        return result([], HOME, complete=False,status='NEEDS_USER_ACTION', candidates=location,
                      reason='SELECT_EXACT_ROOM' if location else 'ROOMS_NOT_VERIFIED', scene=scene)
    room = chosen[0]
    number, size = params.get('page',1), params.get('limit',20)
    requested = params.get('mode') or scene['mode']
    modes = [requested] if requested else list(MODES)
    for mode in modes:
        payload = {'sceneUuid':scene['scene_id'],'siteType':scene['site_type'],'classTypeEnum':'ROOM',
                   'classTypeUuid':room['room_id'],'reserveDate':day,'sceneUseType':mode,'resvKind':'CURRENT_RESERVE',
                   'searchValue':'','siteKindId':'','pageNum':number,'pageSize':size}
        raw = reader.call(PAGE, payload)
        rows, scope = page_rows(raw,number,size,'uuid')
        if rows or scope['upstream_total'] > 0:
            items = [field_row(x,scene['scene_id'],mode,day,room['room_id']) for x in rows]
            incomplete = any(not x['sessions'] or any(s['available'] is None for s in x['sessions']) for x in items)
            return result(items,HOME,**scope,date=day,scene=scene,room=room,mode=mode,
                          complete=not incomplete,warnings=['UNVERIFIED_SESSION_OR_FREE_PERIOD'] if incomplete else [],
                          scope='ONE_ROOM_DATE_MODE_AND_UPSTREAM_PAGE')
    return result([],HOME,complete=False,status='PARTIAL',warnings=['EMPTY_AVAILABILITY_UNVERIFIED'],
                  scene=scene,room=room,date=day,checked_modes=modes,scope='NO_VERIFIED_FIELDS')


def normalize_order(row):
    rid = ident(row.get('resvUuid'))
    time_range = row.get('resvTime') or {}
    start, end = stamp(time_range.get('startTime')), stamp(time_range.get('endTime'))
    sites = row.get('siteVoList')
    require(isinstance(sites,list) and start and end and end > start,'SPORTS_ORDER_FORMAT_CHANGED')
    extended = row.get('resvExtendStatus')
    approval, phase = row.get('resvCheckStatus'), row.get('resvStatus')
    payment = PAYMENT.get(row.get('orderStatus'),'UNKNOWN')
    if type(extended) is int and extended & 2:
        state = 'CANCELLED'
    elif phase == 'RESV_END':
        state = 'COMPLETED'
    elif approval in ('RESV_WAIT_APPROVE','RESV_APPROVING'):
        state = 'PENDING_APPROVAL'
    elif payment in ('PENDING_PAYMENT','PROCESSING'):
        state = 'PENDING_PAYMENT'
    elif approval == 'RESV_APPROVE_PASS' and phase in ('RESV_EFFECT','RESV_UNEFFECT') and payment not in ('PAYMENT_EXPIRED','PAYMENT_CANCELLED'):
        state = 'SCHEDULED'
    else:
        state = 'UNKNOWN'
    return {'reservation_id':rid,'scene_id':ident(row.get('sceneUuid')),'scene_name':label(row.get('sceneName')),
            'sites':[{'site_id':ident(x.get('uuid')),'name':label(x.get('siteName'))} for x in sites],
            'start_at':start,'end_at':end,'status':state,'platform_status':phase,'approval_status':approval,
            'extended_status':extended,'payment_status':payment,'refund_status':REFUND.get(row.get('orderRefundStatus'),'UNKNOWN'),
            'paid_amount_fen':row.get('paidAmount'),'payment_deadline':stamp(row.get('paymentDeadline')),
            'cancel_before_start_minutes':row.get('cancelBeforeStartTime'),'mode':row.get('sceneUseType'),
            'official_url':PERSONAL}


def order_page(reader, number, size):
    raw = reader.call(RECORDS, {'pageNum':number,'pageSize':size,'containsActivity':False,
                                'orderItems':'resvBeginTime','orderRule':'desc'})
    rows, scope = page_rows(raw,number,size,'resvUuid')
    return rows, scope


def identity_fields(scene, sites, span, members):
    require(isinstance(sites,list) and sites and isinstance(span,dict) and isinstance(members,list) and members,
            'SPORTS_DETAIL_MISMATCH')
    return ((ident(scene) if scene else None), tuple(sorted(ident(x.get('uuid')) for x in sites)),
            stamp(span.get('startTime')),stamp(span.get('endTime')),
            tuple(sorted(ident(x.get('userId')) for x in members)))


def row_identity(row):
    return identity_fields(row.get('sceneUuid'),row.get('siteVoList'),row.get('resvTime'),row.get('resvMemberVos'))


def own_order(reader, record_id):
    # Some PC detail responses leave resvUuid blank. In that case require an
    # exact, unique match over all own records, independent of the request URL.
    seen = {}
    expected_total = None
    for number in range(1,51):
        rows, scope = order_page(reader,number,50)
        require(expected_total in (None,scope['upstream_total']), 'SPORTS_ORDER_LIST_CHANGED')
        expected_total = scope['upstream_total']
        for row in rows:
            rid = ident(row.get('resvUuid'))
            require(rid not in seen,'SPORTS_ORDER_LIST_CHANGED')
            seen[rid] = row
        if not scope['next_page']:
            break
    else:raise CampusError('SPORTS_ORDER_SEARCH_INCOMPLETE')
    if record_id not in seen:raise CampusError('SPORTS_OWN_RECORD_NOT_FOUND')
    row = seen[record_id]
    detail = reader.call(DETAIL, {'resvUuid':record_id}).get('data')
    require(isinstance(detail,dict) and isinstance((detail.get('data') or {}).get('moduleData'),dict),
            'SPORTS_DETAIL_MISMATCH')
    if detail.get('resvUuid'):
        require(str(detail['resvUuid'])==record_id,'SPORTS_DETAIL_MISMATCH')
    else:
        module = detail['data']['moduleData']
        sites = module.get('resvSite')
        spans = module.get('resvTime')
        require(isinstance(sites,list) and sites and isinstance(spans,list) and len(spans)==1,'SPORTS_DETAIL_MISMATCH')
        require(all(x.get('sceneUuid')==sites[0].get('sceneUuid') for x in sites),'SPORTS_DETAIL_MISMATCH')
        signature = identity_fields(sites[0].get('sceneUuid'),sites,spans[0],module.get('resvMember'))
        # The detail page may omit the scene as well as the record UUID.
        # Count candidates across all scenes using only returned fields.
        def matches(candidate):
            identity = row_identity(candidate)
            return identity[1:]==signature[1:] and (signature[0] is None or identity[0]==signature[0])
        require(matches(row),'SPORTS_DETAIL_MISMATCH')
        require(sum(matches(candidate) for candidate in seen.values())==1,'SPORTS_DETAIL_AMBIGUOUS')
    return row,detail


def query(browsers, operation, params):
    if operation == 'availability':
        if not (params.get('scene_id') or params.get('name')):
            raise CampusError('SPORTS_SCENE_SELECTOR_REQUIRED')
        local_day(params.get('date'))
    with Reader(browsers) as reader:
        if operation == 'scenes':
            return scenes(reader, params.get('query',''))
        if operation == 'availability':
            return availability(reader, params)
        if operation == 'orders':
            rows, scope = order_page(reader,params.get('page',1),params.get('limit',20))
            return result([normalize_order(x) for x in rows],PERSONAL,**scope,scope='OWN_RESERVATIONS_REQUESTED_PAGE')
        if operation == 'order_detail':
            row, detail = own_order(reader,params['record_id'])
            item = normalize_order(row)
            data = detail['data']
            require(row.get('resvStatus') == data.get('resvRecStatus') and row.get('resvExtendStatus') == data.get('resvExtendStatus'),
                    'SPORTS_RECORD_CHANGED_REFRESH')
            item.update(detail_verified=True,order_info_present=isinstance(detail.get('orderInfo'),dict))
            return result([item],PERSONAL,scope='VERIFIED_OWN_RECORD_AND_DETAIL')
    raise CampusError('UNREGISTERED_QUERY_OPERATION')

