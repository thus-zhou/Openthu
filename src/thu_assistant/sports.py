"""Typed sports reads over the official PC frontend, with explicit upstream windows."""
from datetime import date, datetime, time, timedelta, timezone
import html
import re

from .bridge import CampusError
from . import domain_models
from .sports_transport import Reader, SCENE, LOCATION, PAGE, RECORDS, DETAIL, HOME, PERSONAL

VERSION = 'sports-v1'
TZ = timezone(timedelta(hours=8))
MODES = ('SPORT_GROUP', 'SPORT_PERSON')
PAYMENT = {1:'PENDING_PAYMENT', 2:'PROCESSING', 4:'PAID', 8:'PAYMENT_CANCELLED', 16:'PAYMENT_EXPIRED'}
REFUND = {0:'NONE', 1:'REQUESTED', 2:'REJECTED', 4:'REFUNDING', 8:'REFUNDED', 16:'FAILED', 128:'PENDING', 256:'MANUAL'}
APPROVAL = {'RESV_WAIT_APPROVE':'PENDING', 'RESV_APPROVING':'PENDING', 'RESV_APPROVE_PASS':'APPROVED'}
RESERVATION = {'RESV_EFFECT':'EFFECTIVE', 'RESV_UNEFFECT':'INEFFECTIVE', 'RESV_END':'ENDED'}
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


def fen_display(value):
    """Exact fen-to-yuan text; never a float and never a second *100."""
    return f'{value // 100}.{value % 100:02d}' if type(value) is int and value >= 0 else None


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


def page_rows(raw, page, size, key, strict=True):
    """Validate one upstream page.

    ``strict`` is the legacy behaviour used by write reconciliation.  Read
    operations pass ``strict=False`` so a short or repeated page is returned as
    a partial page with its continuation instead of being hidden behind an
    exception.  A structurally wrong payload still fails closed.
    """
    rows = array(raw)
    total = raw.get('count')
    require(type(total) is int and total >= 0, 'SPORTS_PAGINATION_CHANGED')
    expected = max(0, min(size, total - (page - 1) * size))
    complete = len(rows) == expected
    warnings = []
    if not complete:
        if strict:
            raise CampusError('SPORTS_PAGINATION_CHANGED')
        warnings.append('UPSTREAM_PAGE_COUNT_MISMATCH')
    ids = [ident(x.get(key)) for x in rows]
    if len(set(ids)) != len(ids):
        if strict:
            raise CampusError('SPORTS_DUPLICATE_RECORD')
        unique_rows, seen = [], set()
        for row in rows:
            record_id = ident(row.get(key))
            if record_id in seen:
                continue
            seen.add(record_id)
            unique_rows.append(row)
        rows = unique_rows
        ids = [ident(x.get(key)) for x in rows]
        complete = False
        warnings.append('SPORTS_DUPLICATE_RECORD')
    return rows, {'upstream_page':page,'upstream_page_size':size,'upstream_total':total,
                  'next_page':(page+1 if page*size < total else None) if complete else page,
                  'complete':complete,'warnings':sorted(set(warnings)),
                  'expected_rows':expected,'returned_rows':len(rows)}


def collect_pages(read_page, key, *, max_pages=50):
    """Read every page of one own-record list without hiding a partial read.

    Returns the rows read so far and the page to read next (possibly the same
    page when the source returned it short).  A repeated page or a changed
    upstream total stops the walk instead of appending the same record twice.
    """
    rows = []
    seen = set()
    total = None
    pages = 0
    warnings = []
    next_page = None
    for number in range(1, max_pages + 1):
        block, scope = read_page(number)
        pages += 1
        if total is not None and scope['upstream_total'] != total:
            warnings.append('UPSTREAM_TOTAL_CHANGED')
            return {'rows': rows, 'complete': False, 'warnings': sorted(set(warnings)),
                    'pages_read': pages, 'upstream_total': total, 'next_page': number}
        total = scope['upstream_total']
        current = {ident(row.get(key)) for row in block}
        if seen & current:
            warnings.append('REPEATED_OWN_RECORD_PAGE')
            return {'rows': rows, 'complete': False, 'warnings': sorted(set(warnings)),
                    'pages_read': pages, 'upstream_total': total, 'next_page': number}
        seen.update(current)
        rows.extend(block)
        warnings.extend(scope.get('warnings') or [])
        if scope.get('next_page') is None:
            complete = scope.get('complete', True) and len(rows) == total
            if len(rows) != total and 'UPSTREAM_PAGE_COUNT_MISMATCH' not in warnings:
                warnings.append('UPSTREAM_PAGE_COUNT_MISMATCH')
            return {'rows': rows, 'complete': complete, 'warnings': sorted(set(warnings)),
                    'pages_read': pages, 'upstream_total': total, 'next_page': None}
        next_page = scope['next_page']
        if not scope.get('complete', True):
            return {'rows': rows, 'complete': False, 'warnings': sorted(set(warnings)),
                    'pages_read': pages, 'upstream_total': total, 'next_page': next_page}
    warnings.append('OWN_RECORD_PAGE_LIMIT')
    return {'rows': rows, 'complete': False, 'warnings': sorted(set(warnings)),
            'pages_read': pages, 'upstream_total': total, 'next_page': next_page}


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
                         'unit_fee_fen':cents, 'unit_fee_display':fen_display(cents),
                         'quoted_total_fen':total, 'quoted_total_display':fen_display(total),
                         'fee_unit':'FEN', 'currency':'CNY','charging_mode':fee.get('chargingMode'),
                         'payment_hint':fee.get('payType'),'capacity':entry.get('allowUserNum'),
                         'capacity_unit':'PERSON','capacity_scope':'SESSION_ALLOW_USER_NUM',
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
        page_complete = scope.pop('complete', True)
        page_warnings = scope.pop('warnings', [])
        if rows or scope['upstream_total'] > 0:
            items = [field_row(x,scene['scene_id'],mode,day,room['room_id']) for x in rows]
            incomplete = any(not x['sessions'] or any(s['available'] is None for s in x['sessions']) for x in items)
            return result(items,HOME,**scope,date=day,scene=scene,room=room,mode=mode,
                          complete=page_complete and not incomplete,
                          warnings=page_warnings + (['UNVERIFIED_SESSION_OR_FREE_PERIOD'] if incomplete else []),
                          scope='ONE_ROOM_DATE_MODE_AND_UPSTREAM_PAGE')
    return result([],HOME,complete=False,status='PARTIAL',warnings=['EMPTY_AVAILABILITY_UNVERIFIED'],
                  scene=scene,room=room,date=day,checked_modes=modes,scope='NO_VERIFIED_FIELDS')


def axis(value, mapping):
    """One source status axis; an unfamiliar value keeps UNKNOWN plus a digest."""
    if value is None:
        return {'status': 'UNKNOWN', 'mapping_state': 'MISSING',
                'source_status_digest': None}
    mapped = domain_models.mapped_status(value, mapping)
    return {'status': mapped['status'], 'mapping_state': mapped['mapping_state'],
            'source_status_digest': mapped['source_status_digest']}


def normalize_order(row):
    rid = ident(row.get('resvUuid'))
    time_range = row.get('resvTime') or {}
    start, end = stamp(time_range.get('startTime')), stamp(time_range.get('endTime'))
    sites = row.get('siteVoList')
    require(isinstance(sites,list) and start and end and end > start,'SPORTS_ORDER_FORMAT_CHANGED')
    extended = row.get('resvExtendStatus')
    approval, phase = row.get('resvCheckStatus'), row.get('resvStatus')
    payment = PAYMENT.get(row.get('orderStatus'),'UNKNOWN')
    refund = REFUND.get(row.get('orderRefundStatus'),'UNKNOWN')
    axes = {'reservation': axis(phase, RESERVATION),
            'approval': axis(approval, APPROVAL),
            'payment': axis(row.get('orderStatus'), PAYMENT),
            'refund': axis(row.get('orderRefundStatus'), REFUND)}
    axes_confirmed = all(value['mapping_state'] == 'MAPPED' for value in axes.values())
    if type(extended) is int and extended & 2:
        state = 'CANCELLED'
    elif not axes_confirmed:
        # An unfamiliar status axis cannot support an overall success-like
        # state, even when another axis still looks effective.
        state = 'UNKNOWN'
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
    paid, total = row.get('paidAmount'), row.get('totalAmount')
    require(paid is None or type(paid) is int and paid >= 0, 'SPORTS_FEE_FORMAT_CHANGED')
    require(total is None or type(total) is int and total >= 0, 'SPORTS_FEE_FORMAT_CHANGED')
    return {'reservation_id':rid,'scene_id':ident(row.get('sceneUuid')),'scene_name':label(row.get('sceneName')),
            'sites':[{'site_id':ident(x.get('uuid')),'name':label(x.get('siteName'))} for x in sites],
            'start_at':start,'end_at':end,'status':state,'state_confirmed':axes_confirmed,
            'platform_status':phase,'approval_status':approval,
            'extended_status':extended,'payment_status':payment,'refund_status':refund,
            'status_axes':axes,'status_axes_confirmed':axes_confirmed,
            'paid_amount_fen':paid,'paid_amount_display':fen_display(paid),
            'total_amount_fen':total,'total_amount_display':fen_display(total),
            'fee_unit':'FEN','payment_receipt_verified':False,
            'payment_deadline':stamp(row.get('paymentDeadline')),
            'cancel_before_start_minutes':row.get('cancelBeforeStartTime'),'mode':row.get('sceneUseType'),
            'official_url':PERSONAL}


def order_page(reader, number, size, strict=True):
    raw = reader.call(RECORDS, {'pageNum':number,'pageSize':size,'containsActivity':False,
                                'orderItems':'resvBeginTime','orderRule':'desc'})
    rows, scope = page_rows(raw,number,size,'resvUuid',strict=strict)
    return rows, scope


def identity_fields(scene, sites, span, members):
    require(isinstance(sites,list) and sites and isinstance(span,dict) and isinstance(members,list) and members,
            'SPORTS_DETAIL_MISMATCH')
    return ((ident(scene) if scene else None), tuple(sorted(ident(x.get('uuid')) for x in sites)),
            stamp(span.get('startTime')),stamp(span.get('endTime')),
            tuple(sorted(ident(x.get('userId')) for x in members)))


def row_identity(row):
    return identity_fields(row.get('sceneUuid'),row.get('siteVoList'),row.get('resvTime'),row.get('resvMemberVos'))


def order_detail_for(reader, row, candidates):
    # Some PC detail responses leave resvUuid blank. In that case require an
    # exact, unique match over all own records, independent of the request URL.
    detail = reader.call(DETAIL, {'resvUuid':ident(row.get('resvUuid'))}).get('data')
    require(isinstance(detail,dict) and isinstance((detail.get('data') or {}).get('moduleData'),dict),
            'SPORTS_DETAIL_MISMATCH')
    if detail.get('resvUuid'):
        require(str(detail['resvUuid'])==ident(row.get('resvUuid')),'SPORTS_DETAIL_MISMATCH')
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
        require(sum(matches(candidate) for candidate in candidates)==1,'SPORTS_DETAIL_AMBIGUOUS')
    return detail


def own_order_read(reader, record_id):
    """Read one own order with explicit partial-search semantics.

    The read operation keeps the records it could verify and reports the page
    that should be continued.  It never turns a repeated or short page into a
    claim that the record does not exist.
    """
    collection = collect_pages(lambda number: order_page(reader, number, 50, strict=False),
                               'resvUuid')
    seen = {ident(row.get('resvUuid')): row for row in collection['rows']}
    if record_id not in seen:
        return {'state': 'NOT_FOUND' if collection['complete'] else 'SEARCH_PARTIAL',
                'collection': collection}
    row = seen[record_id]
    detail = order_detail_for(reader, row, list(seen.values()))
    return {'state': 'FOUND', 'row': row, 'detail': detail, 'collection': collection}


def own_order(reader, record_id):
    found = own_order_read(reader, record_id)
    if found['state'] == 'SEARCH_PARTIAL':
        raise CampusError('SPORTS_ORDER_SEARCH_INCOMPLETE')
    require(found['state'] == 'FOUND', 'SPORTS_OWN_RECORD_NOT_FOUND')
    require(found['collection']['complete'], 'SPORTS_ORDER_SEARCH_INCOMPLETE')
    return found['row'], found['detail']


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
            number, size = params.get('page',1), params.get('limit',20)
            rows, scope = order_page(reader,number,size,strict=False)
            return result([normalize_order(x) for x in rows],PERSONAL,**scope,
                          scope='OWN_RESERVATIONS_REQUESTED_PAGE',
                          pagination={'kind':'OWN_ORDER_WINDOW','page':number,'page_size':size,
                                      'upstream_total':scope['upstream_total'],
                                      'next_page':scope['next_page'],
                                      'scope':'OWN_RESERVATIONS_REQUESTED_PAGE',
                                      'complete':scope['complete']})
        if operation == 'order_detail':
            found = own_order_read(reader,params['record_id'])
            require(found['state']=='FOUND',
                    'SPORTS_OWN_RECORD_NOT_FOUND' if found['state']=='NOT_FOUND'
                    else 'SPORTS_ORDER_SEARCH_INCOMPLETE')
            row, detail = found['row'], found['detail']
            item = normalize_order(row)
            data = detail['data']
            require(row.get('resvStatus') == data.get('resvRecStatus') and row.get('resvExtendStatus') == data.get('resvExtendStatus'),
                    'SPORTS_RECORD_CHANGED_REFRESH')
            item.update(detail_verified=True,order_info_present=isinstance(detail.get('orderInfo'),dict))
            collection = found['collection']
            return result([item],PERSONAL,scope='VERIFIED_OWN_RECORD_AND_DETAIL',
                          complete=collection['complete'],warnings=collection['warnings'],
                          collection={'pages_read':collection['pages_read'],
                                      'upstream_total':collection['upstream_total'],
                                      'next_page':collection['next_page'],
                                      'complete':collection['complete']},
                          pagination={'kind':'OWN_ORDER_SEARCH','page_size':50,
                                      'upstream_total':collection['upstream_total'],
                                      'next_page':collection['next_page'],
                                      'pages_read':collection['pages_read'],
                                      'complete':collection['complete'],
                                      'scope':'ALL_OWN_RESERVATION_PAGES'})
    raise CampusError('UNREGISTERED_QUERY_OPERATION')

