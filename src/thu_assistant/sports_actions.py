"""Sports reservation previews, single dispatch, and independent order readback.

The write adapter consumes the read side as one precheck object instead of
re-interpreting platform fields.  A plan is bound to the exact snapshot it was
shown from; a later read that cannot reproduce that snapshot invalidates the
plan instead of dispatching at a new price or against a closed session.
"""
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from .actions import RequestSpec, NotDispatched, digest, sports_enabled
from .bridge import CampusError
from . import precheck
from . import sports as s
from .sports_transport import (Reader, WIRE, USER, CAPTCHA, CREATE, CANCEL, ORIGIN, PREFIX,
                               PERSONAL, HOME, SIGN, FORM_BRIEF, redact_official_url)

# A lost create response can be reconciled only inside this window.  It bounds
# how long a later, unrelated order could still be attributed to this plan.
RECONCILE_HORIZON_SECONDS = 24 * 60 * 60
REFRESH_AFTER_SECONDS = 60

# Modes the write adapter has a source contract for.  A mode absent here is
# refused instead of reusing another mode's request body or ticket rules.
MODE_CONTRACT = {
    'SPORT_GROUP': {'create': True, 'quantity_unit': 'SESSION', 'ticket_quantity': 1,
                    'participants': 'SELF_ONLY', 'official_handoff': []},
    'SPORT_PERSON': {'create': True, 'quantity_unit': 'TICKET', 'ticket_quantity': 1,
                     'participants': 'SELF_ONLY',
                     'official_handoff': ['CHOOSE_TICKET_AND_QUANTITY']},
}

# A preview that can no longer be reproduced must invalidate the stored plan.
# These codes mean the source moved under the plan: price, session, member or
# mode changed, an official form/verification appeared, or the order state is
# no longer the one the plan was prepared for.
CHANGED_PREVIEW = frozenset({
    'SPORTS_FEE_CHANGED', 'SPORTS_FEE_NOT_VERIFIED', 'SPORTS_SLOT_UNAVAILABLE',
    'SPORTS_SLOT_NOT_VERIFIED', 'SPORTS_SESSION_NOT_FOUND',
    'SPORTS_SITE_NOT_IN_SELECTED_ROOM', 'SPORTS_MODE_MISMATCH',
    'SPORTS_FORM_DEPLOYMENT_UNKNOWN', 'SPORTS_VERIFICATION_RULE_UNKNOWN',
    'SPORTS_EXISTING_UNPAID_ORDER', 'SPORTS_EXISTING_MATCHING_RESERVATION',
    'SPORTS_CANCELLATION_NOT_AVAILABLE', 'SPORTS_OWN_RECORD_NOT_FOUND',
    'SPORTS_ORDER_SEARCH_INCOMPLETE', 'SPORTS_RECORD_CHANGED_REFRESH',
})

ELIGIBILITY_FLAGS = frozenset({'OFFICIAL_CAPTCHA', 'OFFICIAL_FORM', 'CONFIRM_PARTICIPANTS',
                               'CHOOSE_TICKET_AND_QUANTITY', 'OFFICIAL_CANCEL_CONTROL'})
FEE_FLAGS = frozenset({'CONFIRM_TOTAL_AND_PAYMENT_METHOD', 'CONFIRM_REFUND_CONSEQUENCES'})
RULE_FLAGS = frozenset({'CONFIRM_CANCELLATION_RULE', 'CONFIRM_CANCELLATION_DEADLINE'})
REFUND_ACCEPTED = frozenset({'REQUESTED', 'REFUNDING', 'PENDING', 'MANUAL'})


def capabilities():
    """The sports action capability table handed to the public facade (6.1)."""
    return {'adapter_version': 'sports-action-v1',
            'operations': ['create_reservation', 'cancel_reservation'],
            'modes': {name: dict(value) for name, value in MODE_CONTRACT.items()},
            'amount': {'unit': 'FEN', 'currency': 'CNY', 'quantity_unit_per_mode': True,
                       'total_source': 'SESSION_PAYABLE_AMOUNT_ONLY',
                       'multiplies_unit_price_never': True},
            'reconciliation': {'horizon_seconds': RECONCILE_HORIZON_SECONDS,
                               'order_page_size': 50, 'max_pages': 50,
                               'requires_unique_remote_record': True},
            'official_handoff': sorted(ELIGIBILITY_FLAGS | FEE_FLAGS | RULE_FLAGS),
            'not_supported': ['MULTI_TICKET_PURCHASE', 'AUTOMATIC_PAYMENT',
                              'CAPTCHA_SOLVING', 'IDENTITY_BYPASS',
                              'REPLAY_UNCERTAIN_WRITE']}


def inputs(business, operation):
    keys = ('scene_id', 'room_id', 'site_id', 'session_id', 'date', 'mode', 'members',
            'payment_method') if operation == CREATE else ('record_id',)
    return {k: business[k] for k in keys} | ({'expected_total_fen': business['expected_total_fen']}
                                             if operation == CREATE and 'expected_total_fen' in business else {})


def collect_orders(reader):
    """Read every own-order page without hiding a partial list.

    A short, repeated or changed page stops the walk with ``complete=False`` so
    a caller can never treat a partial list as proof that no similar order
    exists.
    """
    return s.collect_pages(lambda number: s.order_page(reader, number, 50, strict=False),
                           'resvUuid')


def stable_view(block):
    """Time-free projection of one precheck block, safe to bind in a plan."""
    fees = (block.get('fees') or {}).get('items') or []
    rules = (block.get('rules') or {}).get('items') or []
    return {'resource': block.get('resource'), 'window': block.get('window'),
            'availability': block.get('availability'),
            'eligibility': block.get('eligibility'),
            'unknowns': block.get('unknowns'),
            'preparation_state': block.get('preparation_state'),
            'fees': [{'amount_fen': item.get('amount_fen'), 'state': item.get('state'),
                      'raw_field': item.get('raw_field')} for item in fees],
            'rules': [{'raw_field': item.get('raw_field'), 'value': item.get('value'),
                       'unit': item.get('unit'), 'state': item.get('state')} for item in rules]}


def slot_binding(summary, site, slot):
    """Bind the selected site and session, not only the page summary."""
    return digest({'selection': summary.get('selection'),
                   'resource': summary.get('resource'),
                   'site': stable_view(site['precheck']),
                   'session': stable_view(slot['precheck']),
                   'site_fields': {'site_id': site['site_id'], 'scene_id': site['scene_id'],
                                   'room_id': site['room_id'], 'mode': site['mode'],
                                   'site_type': site['site_type'], 'form_id': site['form_id'],
                                   'form_state_known': site['form_state_known']},
                   'session_fields': {'session_id': slot['session_id'],
                                      'start_at': slot['start_at'], 'end_at': slot['end_at'],
                                      'capacity': slot['capacity'],
                                      'reserved_count': slot['reserved_count']}})


def order_binding(block, row, raw):
    """Bind one own order and its current state to a cancellation plan."""
    return digest({'order': stable_view(block),
                   'fields': {'reservation_id': row['reservation_id'],
                              'scene_id': row['scene_id'],
                              'sites': [x['site_id'] for x in row['sites']],
                              'start_at': row['start_at'], 'end_at': row['end_at'],
                              'status': row['status'], 'platform_status': row['platform_status'],
                              'approval_status': row['approval_status'],
                              'payment_status': row['payment_status'],
                              'refund_status': row['refund_status'],
                              'extended_status': row['extended_status'],
                              'property': raw.get('resvProperty'),
                              'paid_fen': row['paid_amount_fen'],
                              'total_fen': row['total_amount_fen'],
                              'cancel_before_start_minutes': row['cancel_before_start_minutes']}})


def selected_slot(reader, arguments):
    page = 1
    selectors = {key: arguments[key] for key in ('scene_id', 'room_id', 'mode', 'date')}
    while page <= 50:
        result = s.availability(reader, {**selectors, 'page': page, 'limit': 50})
        if not result['complete']:
            raise CampusError('SPORTS_SLOT_NOT_VERIFIED')
        built = precheck.build('sports', 'availability', result, result['items'], selectors,
                               datetime.now(s.TZ).isoformat(timespec='seconds'))
        for site in result['items']:
            if site['site_id'] != arguments['site_id']:
                continue
            matches = [x for x in site['sessions'] if x['session_id'] == arguments['session_id']]
            s.require(len(matches) == 1, 'SPORTS_SESSION_NOT_FOUND')
            s.require(sum((x['start_at'], x['end_at']) == (matches[0]['start_at'], matches[0]['end_at'])
                          for x in site['sessions']) == 1, 'SPORTS_SESSION_TIME_AMBIGUOUS')
            # The site and the session must both belong to the requested project
            # and mode; a same-named site in another project is not a match.
            s.require(site['scene_id'] == arguments['scene_id'] and site['mode'] == arguments['mode'],
                      'SPORTS_MODE_MISMATCH')
            return result['scene'], site, matches[0], built['summary']
        if not result.get('next_page'):
            break
        page = result['next_page']
    raise CampusError('SPORTS_SITE_NOT_IN_SELECTED_ROOM')


def scene_mode(reader, args):
    """Check the project's declared mode before any mode-specific page read."""
    catalog = s.scenes(reader, '')
    matches = [x for x in catalog['items'] if x['scene_id'] == args['scene_id']]
    s.require(len(matches) == 1, 'SPORTS_SCENE_SELECTOR_REQUIRED')
    return matches[0]['mode']


def _creation_preview(reader, args):
    mode = args['mode']
    s.require(mode in MODE_CONTRACT and MODE_CONTRACT[mode]['create'], 'INVALID_ARGUMENTS')
    s.require(scene_mode(reader, args) in (None, mode), 'INVALID_ARGUMENTS')
    scene, site, slot, summary = selected_slot(reader, args)
    s.require(scene.get('mode') in (None, mode), 'INVALID_ARGUMENTS')
    s.require(slot['available'] is True and summary['availability']['state'] == 'FREE',
              'SPORTS_SLOT_UNAVAILABLE')
    s.require(slot['unit_fee_fen'] is not None and (slot['precheck']['fees'] or {}).get('state') == 'QUOTED',
              'SPORTS_FEE_NOT_VERIFIED')
    member = reader.call(USER)['data']
    member_id = s.ident(member.get('id'))
    captcha = reader.call(CAPTCHA)['data']
    s.require(captcha.get('sysKey') == 'RESV_CODE' and str(captcha.get('sysValue')) in ('0', '1'),
              'SPORTS_VERIFICATION_RULE_UNKNOWN')
    fee, total = slot['unit_fee_fen'], slot['quoted_total_fen']
    # A free plan may only stay free.  A newly declared fee or total is a
    # changed preview, never a silent upgrade of the amount.
    s.require(args['payment_method'] != 'FREE' or (fee == 0 and total in (None, 0)),
              'SPORTS_FEE_CHANGED')
    if args['payment_method'] == 'FREE' and args.get('expected_total_fen', 0) != 0:
        raise CampusError('SPORTS_FEE_CHANGED')
    checks = []
    if str(captcha['sysValue']) == '1':
        checks.append('OFFICIAL_CAPTCHA')
    deployment = None
    if site['form_id']:
        brief = reader.call(FORM_BRIEF + s.ident(site['form_id'])).get('data')
        s.require(isinstance(brief, dict) and brief.get('deployUuid'), 'SPORTS_FORM_DEPLOYMENT_UNKNOWN')
        deployment = s.ident(brief['deployUuid'])
    if site['form_id'] or not site['form_state_known']:
        checks.append('OFFICIAL_FORM')
    if total is not None and 'expected_total_fen' in args and total != args['expected_total_fen']:
        raise CampusError('SPORTS_FEE_CHANGED')
    # Without a source-declared total the amount is never derived from a unit
    # price times an assumed headcount; the official settlement page decides.
    if args['payment_method'] != 'FREE' or total is None:
        checks.append('CONFIRM_TOTAL_AND_PAYMENT_METHOD')
    checks.extend(MODE_CONTRACT[mode]['official_handoff'])
    if args['members'] != 'self':
        checks.append('CONFIRM_PARTICIPANTS')
    rules = {item['raw_field']: item for item in slot['precheck']['rules'].get('items') or []}
    if (rules.get('cancelBeforeStart') or {}).get('state') != 'DECLARED':
        checks.append('CONFIRM_CANCELLATION_RULE')
    collection = collect_orders(reader)
    s.require(collection['complete'], 'SPORTS_ORDER_SEARCH_INCOMPLETE')
    for raw in collection['rows']:
        order = s.normalize_order(raw)
        if order['payment_status'] == 'PENDING_PAYMENT':
            raise CampusError('SPORTS_EXISTING_UNPAID_ORDER')
        if (order['scene_id'] == args['scene_id'] and order['start_at'] == slot['start_at']
                and args['site_id'] in [x['site_id'] for x in order['sites']]
                and order['status'] not in ('CANCELLED', 'COMPLETED')):
            raise CampusError('SPORTS_EXISTING_MATCHING_RESERVATION')
    quantity = MODE_CONTRACT[mode]['ticket_quantity']
    unit = MODE_CONTRACT[mode]['quantity_unit']
    unit_text, total_text = s.fen_display(fee), s.fen_display(total)
    if total is None:
        amount_text = (f'数量 {quantity} {unit}，总额须在官方结算页确认；'
                       f'来源仅声明单价 {unit_text} 元')
    else:
        amount_text = (f'数量 {quantity} {unit}，来源声明单价 {unit_text} 元，'
                       f'应付总额已核对：{total_text} 元')
    rule_text = ('平台取消提前量（分钟）：' + str(rules['cancelBeforeStart']['value'])
                 if (rules.get('cancelBeforeStart') or {}).get('state') == 'DECLARED'
                 else '取消期限须在官方页面核实')
    business = {**args, 'target': scene['name'] + ' / ' + site['name'],
                'start_time': slot['start_at'], 'end_time': slot['end_at'],
                'account_member': member_id, 'fee': amount_text, 'cancel_rule': rule_text,
                'handoff': ','.join(checks),
                'official_url': redact_official_url(HOME + '#/swim' if mode == 'SPORT_PERSON'
                                                    else HOME + '#/sports'),
                'fingerprint': digest([slot_binding(summary, site, slot), mode,
                                       slot['charging_mode'], fee, total, site['form_id'],
                                       deployment, slot['payment_hint']])}
    span = {'startTime': slot['start_at'][:19].replace('T', ' '),
            'endTime': slot['end_at'][:19].replace('T', ' ')}
    body = {'sceneUuid': args['scene_id'], 'siteUuid': args['site_id'], 'siteType': site['site_type'],
            'sceneUseType': args['mode'], 'reserveTime': [span],
            'siteSessionReserve': [{'sessionDetailUuid': args['session_id'], 'reserveTime': span}],
            'resvMember': [member_id], 'resvKind': 'CURRENT_RESERVE',
            'payType': 'PAY_ONLINE' if args['payment_method'] == 'FREE' else args['payment_method'],
            'purchaseUuid': '',
            'formParam': {'formId': '', 'deployUuid': '', 'variables': {}, 'chooseCandidates': {}},
            'captcha': ''}
    base_checks = {
        'availability': summary['availability']['state'] == 'FREE'
        and summary['availability']['source_complete'] is True
        and slot['precheck']['availability']['state'] == 'FREE',
        'eligibility': True,
        'fees': (slot['precheck']['fees'] or {}).get('state') == 'QUOTED',
        'rules': (rules.get('cancelBeforeStart') or {}).get('state') == 'DECLARED',
    }
    return business, body, base_checks


def creation_preview(reader, args):
    business, body, _ = _creation_preview(reader, args)
    return business, body


def _cancel_preview(reader, args):
    found = s.own_order_read(reader, args['record_id'])
    if found['state'] == 'NOT_FOUND':
        raise CampusError('SPORTS_OWN_RECORD_NOT_FOUND')
    if found['state'] == 'SEARCH_PARTIAL' or not found['collection']['complete']:
        raise CampusError('SPORTS_ORDER_SEARCH_INCOMPLETE')
    raw, detail = found['row'], found['detail']
    row = s.normalize_order(raw)
    s.require(detail['data'].get('resvExtendStatus') == row['extended_status'],
              'SPORTS_RECORD_CHANGED_REFRESH')
    s.require(row['status'] in ('SCHEDULED', 'PENDING_PAYMENT', 'PENDING_APPROVAL')
              and datetime.fromisoformat(row['start_at']) > datetime.now(s.TZ),
              'SPORTS_CANCELLATION_NOT_AVAILABLE')
    # Refunds, individual tickets, and uncertain controls belong on the
    # official confirmation page where the platform presents exact consequences.
    handoff = []
    if (row['platform_status'] != 'RESV_UNEFFECT' and row['approval_status'] != 'RESV_WAIT_APPROVE'
            or type(row['extended_status']) is int and row['extended_status'] & 256
            or row['payment_status'] == 'PROCESSING'):
        raise CampusError('SPORTS_CANCELLATION_NOT_AVAILABLE')
    if row['mode'] != 'SPORT_GROUP' or type(raw.get('resvProperty')) is not int or raw['resvProperty'] != 0:
        handoff.append('OFFICIAL_CANCEL_CONTROL')
    paid = row['paid_amount_fen']
    if (type(paid) is not int or paid != 0 or row['refund_status'] != 'NONE'
            or row['payment_status'] in ('UNKNOWN', 'PROCESSING')):
        handoff.append('CONFIRM_REFUND_CONSEQUENCES')
    block = precheck.build('sports', 'order_detail',
                           s.result([dict(row)], s.PERSONAL, complete=True), [dict(row)],
                           {'record_id': row['reservation_id']},
                           datetime.now(s.TZ).isoformat(timespec='seconds'))['items'][0]
    rules = {item['raw_field']: item for item in block['rules'].get('items') or []}
    cutoff = row['cancel_before_start_minutes']
    valid_cutoff = (rules.get('cancelBeforeStart') or {}).get('state') == 'DECLARED' \
        and type(cutoff) is int and 0 <= cutoff <= 525600
    if not valid_cutoff or datetime.now(s.TZ) >= datetime.fromisoformat(row['start_at']) - timedelta(minutes=cutoff):
        handoff.append('CONFIRM_CANCELLATION_DEADLINE')
    business = {**args, 'target': row['scene_name'] + ' / ' + '、'.join(x['name'] for x in row['sites']),
                'scene_id': row['scene_id'], 'site_ids': json.dumps([x['site_id'] for x in row['sites']]),
                'start_time': row['start_at'], 'end_time': row['end_at'],
                'original_status': row['status'],
                'fee': (f'已付 {s.fen_display(paid)} 元；付款状态 {row["payment_status"]}'
                        if type(paid) is int else '已付金额待官方核实'),
                'refund_state': row['refund_status'],
                'cancel_rule': (f'平台取消提前分钟数 {cutoff}；退款结果独立核对' if valid_cutoff
                                else '取消期限须在官方页面核实；退款结果独立核对'),
                'official_url': redact_official_url(row['official_url']),
                'handoff': ','.join(handoff), 'fingerprint': order_binding(block, row, raw)}
    base_checks = {'availability': True, 'eligibility': True,
                   'fees': True, 'rules': valid_cutoff}
    return business, {'resvUuid': args['record_id']}, base_checks


def cancel_preview(reader, args):
    business, body, _ = _cancel_preview(reader, args)
    return business, body


def dispatch_exact(reader, path, body, gate):
    """Bind the official signed URL to the one allowed unsigned business request."""
    if path not in (CREATE,CANCEL):
        raise NotDispatched()
    prepared = reader.page.evaluate(WIRE, {'path':path,'method':'POST','params':body,'prepare':True})
    url = urlsplit(prepared['url'])
    canonical = ORIGIN+PREFIX+path
    if (url.scheme+'://'+url.netloc+url.path != canonical or set(k for k,_ in parse_qsl(url.query)) != SIGN
            or json.loads(prepared['body']) != body):
        raise NotDispatched()
    def guard(route):
        req = route.request
        if (req.url == prepared['url'] and req.frame == reader.page.main_frame and
                gate.consume(canonical,req.method,req.post_data)):
            return route.continue_()
        return route.abort()
    # One short-lived page route, bound to the exact signed URL, method, frame
    # and body. Never grant an origin-wide exception or use an HTTP retry.
    pattern = re.compile('^'+re.escape(prepared['url'])+'$')
    reader.page.route(pattern,guard)
    try:
        reader.page.evaluate("""async () => {
          const p=window.__thuSportPending;delete window.__thuSportPending;
          const c=new AbortController(),t=setTimeout(()=>c.abort(),18000);
          try{await fetch(p.url,{...p.init,signal:c.signal})}finally{clearTimeout(t)}
        }""")
    finally:
        reader.page.unroute(pattern,guard)
    if not gate.sent:
        raise NotDispatched()


class SportsAction:
    version = 'sports-action-v1'
    accepts_ledger = True
    effect_step = 'official_request'

    def __init__(self, browsers, path):
        self.browsers, self.path = browsers, path

    def read_preview(self, reader, args):
        fn = _creation_preview if self.path == CREATE else _cancel_preview
        return fn(reader, inputs(args, self.path))

    def preview(self, args):
        with Reader(self.browsers) as reader:
            return self.read_preview(reader, args)[0]

    def changed(self, record, code):
        """Signal a preview that can no longer be reproduced.

        The engine compares the business digest and invalidates the stored
        plan; no request is built or sent.
        """
        business = dict(record['business'])
        business['fingerprint'] = digest([business.get('fingerprint'), 'PREVIEW_CHANGED', code])
        return {'business': business, 'context': None, 'checks': {},
                'official_handoff': None, 'requests': []}

    def preflight(self, record):
        s.require(sports_enabled(), 'SPORTS_WRITES_DISABLED')
        with Reader(self.browsers) as reader:
            s.require(reader.session['account_id'] == record['account'], 'ACTION_ACCOUNT_MISMATCH')
            try:
                business, body, base = self.read_preview(reader, record['business'])
            except CampusError as exc:
                if exc.code in CHANGED_PREVIEW:
                    return self.changed(record, exc.code)
                raise
        flags = set(business['handoff'].split(',')) if business['handoff'] else set()
        checks = {'availability': base['availability'],
                  'eligibility': base['eligibility'] and not bool(flags & ELIGIBILITY_FLAGS),
                  'fees': base['fees'] and not bool(flags & FEE_FLAGS),
                  'rules': base['rules'] and not bool(flags & RULE_FLAGS)}
        return {'business': business, 'context': None, 'checks': checks,
                'official_handoff': business['handoff'] or None,
                'requests': [RequestSpec(ORIGIN + PREFIX + self.path, 'POST',
                                         json.dumps(body, ensure_ascii=False))]}

    def dispatch(self, record, gate, ledger=None):
        with Reader(self.browsers) as reader:
            if not sports_enabled() or reader.session['account_id'] != record['account']:
                raise NotDispatched()
            business, body = self.read_preview(reader, record['business'])[:2]
            if (business != record['business'] or business['handoff'] or time.time() >= record['expires_at']
                    or time.time() >= record['authorization']['evidence']['expires_at']):
                raise NotDispatched()
            # Record the dispatch fact before the request can leave the page:
            # NOT_SENT while preparing, then SENT once the gate accepted the
            # exact request.  A lost response keeps SENT, never "not sent".
            if ledger:
                ledger.declare(self.effect_step)
            try:
                dispatch_exact(reader, self.path, body, gate)
            except BaseException:
                if ledger and gate.sent:
                    ledger.sent(self.effect_step)
                raise
            if ledger and gate.sent:
                ledger.sent(self.effect_step)

    def reconcile(self, record):
        with Reader(self.browsers) as reader:
            s.require(reader.session['account_id'] == record['account'], 'ACTION_ACCOUNT_MISMATCH')
            if self.path == CANCEL:
                return self._reconcile_cancel(reader, record)
            return self._reconcile_create(reader, record)

    def _reconcile_cancel(self, reader, record):
        business = record['business']
        found = s.own_order_read(reader, business['record_id'])
        if found['state'] == 'NOT_FOUND':
            return {'records': [], 'complete': True}
        if found['state'] == 'SEARCH_PARTIAL' or not found['collection']['complete']:
            return {'records': [], 'complete': False}
        try:
            row = s.normalize_order(found['row'])
        except CampusError:
            return {'records': [], 'complete': False}
        detail = found['detail']
        if not (row['status'] == 'CANCELLED'
                and detail['data'].get('resvExtendStatus') == row['extended_status']
                and row['scene_id'] == business['scene_id']
                and row['start_at'] == business['start_time']
                and row['end_at'] == business['end_time']
                and [x['site_id'] for x in row['sites']] == json.loads(business['site_ids'])):
            # The record exists but is not (yet) the cancelled one this plan
            # asked for.  Keep the plan unresolved; never infer from a button
            # or an empty list.
            return {'records': [], 'complete': True}
        return {'records': [self.evidence(record, row, 'SUCCEEDED',
                                          source_complete=found['collection']['complete'])],
                'complete': True}

    def _reconcile_create(self, reader, record):
        business = record['business']
        collection = collect_orders(reader)
        records, plausible, unverified = [], 0, 0
        for raw in collection['rows']:
            try:
                row = s.normalize_order(raw)
            except CampusError:
                unverified += 1
                continue
            if not self.matches(record, raw, row):
                continue
            plausible += 1
            try:
                detail = s.order_detail_for(reader, raw, collection['rows'])
            except CampusError:
                unverified += 1
                continue
            if (detail['data'].get('resvExtendStatus') != row['extended_status']
                    or detail['data'].get('resvRecStatus') != row['platform_status']):
                unverified += 1
                continue
            charge = detail.get('orderInfo') if isinstance(detail.get('orderInfo'), dict) else {}
            total = charge.get('payableAmount', raw.get('totalAmount'))
            expected = business.get('expected_total_fen',
                                    0 if business['payment_method'] == 'FREE' else None)
            if type(total) is not int or total < 0:
                unverified += 1
                continue
            # A plan without a declared total was completed on the official
            # page; the platform amount is recorded but cannot be compared to
            # a number this plan never had.
            if expected is not None and total != expected:
                unverified += 1
                continue
            if (business['payment_method'] != 'FREE'
                    and charge.get('payType', raw.get('payType')) != business['payment_method']):
                unverified += 1
                continue
            if (business['payment_method'] != 'FREE' and row['status'] == 'SCHEDULED'
                    and row['payment_status'] != 'PAID'):
                unverified += 1
                continue
            state = {'PENDING_PAYMENT': 'PENDING_PAYMENT', 'PENDING_APPROVAL': 'PENDING_APPROVAL',
                     'SCHEDULED': 'SUCCEEDED', 'CANCELLED': 'FAILED'}.get(row['status'])
            if not state:
                unverified += 1
                continue
            records.append(self.evidence(record, row, state,
                                         source_complete=collection['complete'],
                                         total_verified=expected is not None))
        # Exactly one matching remote record may decide the plan.  A partial
        # list, a second similar record, or a detail that does not agree keeps
        # the result unknown instead of choosing an arbitrary row.
        complete = (collection['complete'] and unverified == 0
                    and plausible == len(records) and len(records) <= 1)
        return {'records': records, 'complete': complete}

    @staticmethod
    def matches(record, raw, row):
        b = record['business']
        if (row['scene_id'] != b['scene_id'] or row['start_at'] != b['start_time']
                or row['end_at'] != b['end_time']
                or [x['site_id'] for x in row['sites']] != [b['site_id']]):
            return False
        # A declared mode must agree with the plan; when the row omits it, the
        # scene, site, exact time and member binding still identify the order.
        if row['mode'] is not None and row['mode'] != b['mode']:
            return False
        members = raw.get('resvMemberVos') or []
        if b['members'] != 'self' or len(members) != 1 or str(members[0].get('userId')) != b['account_member']:
            return False
        if b['mode'] == 'SPORT_PERSON' and members[0].get('spotsNum') != 1:
            return False
        created = s.stamp(raw.get('gmtCreate'))
        dispatched = record.get('dispatch_at')
        if not created or not isinstance(dispatched, (int, float)):
            return False
        # A delayed order may appear after the create response was lost, so the
        # plan expiry is not the reconciliation bound.  The horizon keeps a
        # much later, unrelated order from being attributed to this plan.
        created_ts = datetime.fromisoformat(created).timestamp()
        return (dispatched - 1 <= created_ts <= dispatched + RECONCILE_HORIZON_SECONDS
                and created_ts <= time.time() + 60)

    @staticmethod
    def evidence(record, row, state, *, source_complete, total_verified=False):
        checked = datetime.now(s.TZ).isoformat(timespec='seconds')
        url = redact_official_url(record['business'].get('official_url'))
        refund = row['refund_status']
        return {'account': record['account'], 'operation': record['operation'],
                'business': record['business'],
                'service_id': 'sports', 'record_id': row['reservation_id'], 'state': state,
                'platform': {
                    'reservation_id': row['reservation_id'],
                    'status': row['status'], 'state_confirmed': row['state_confirmed'],
                    'platform_status': row['platform_status'],
                    'approval_status': row['approval_status'],
                    'payment_status': row['payment_status'],
                    'payment_deadline': row['payment_deadline'],
                    'payment_deadline_state': ('PLATFORM_DECLARED' if row['payment_deadline']
                                               else 'NOT_REPORTED'),
                    'payment_receipt_verified': False,
                    'refund_status': refund,
                    'refund_accepted': refund in REFUND_ACCEPTED,
                    'refund_settled_by_platform': refund == 'REFUNDED',
                    # The platform can report a refund without any bank
                    # receipt; the adapter never claims the money arrived.
                    'refund_arrived_verified': False,
                    'amounts': {'unit': 'FEN', 'currency': 'CNY',
                                'paid_fen': row['paid_amount_fen'],
                                'total_fen': row['total_amount_fen'],
                                'total_verified': bool(total_verified),
                                'expected_total_fen': record['business'].get('expected_total_fen')},
                    'official_url': url, 'checked_at': checked,
                    'source_complete': bool(source_complete),
                    'refresh': {'operation': 'sports.order_detail',
                                'record_id': row['reservation_id'],
                                'suggested_after_seconds': REFRESH_AFTER_SECONDS,
                                'official_url': url}}}


def native_grant(record):
    # Only an actual human response in the local Windows desktop can produce a
    # receipt. Tool arguments, source documents and site content cannot do so.
    from . import library_contracts as library
    is_library=record.get('adapter_version')==library.VERSION
    if os.name!='nt' or not (is_library and library.enabled() or record.get('adapter_version')=='sports-action-v1' and sports_enabled()):
        return None
    import ctypes
    b = record['business']
    fields = {'target':'场地','record_id':'要取消的预约号','session_id':'场次编号','start_time':'开始','end_time':'结束',
              'members':'参加者（self 为本人）','fee':'平台费用','expected_total_fen':'指定总额（分）','payment_method':'付款方式',
              'cancel_rule':'取消条件','refund_state':'退款状态','handoff':'需要在官方页面处理'}
    if is_library:
        fields.update(date='日期',area_id='区域',seat_id='座位编号',room_id='房间编号',purpose='研讨主题',headcount='参加人数',rules_summary='平台规则摘要')
    summary = '\n'.join(label+'　'+' '.join(str(b[k]).split())[:4000 if is_library and k in ('members','cancel_rule') else 250] for k,label in fields.items() if b.get(k) is not None)
    text = ('创建预约' if record['operation'].startswith('create_') else '取消预约')+'\n'+summary+'\n\n计划 '+record['operation_id']
    milliseconds = max(0,int((record['expires_at']-time.time())*1000))
    if not milliseconds:return None
    if ctypes.windll.user32.MessageBoxTimeoutW(None,text,'清华助手 · 图书馆办理' if is_library else '清华助手 · 体育办理',0x124,0,milliseconds)!=6:
        return None
    return {'plan_id':record['operation_id'],'account':record['account'],'digest':record['digest'],
            'adapter_version':record['adapter_version'],'source':'human','reference':uuid.uuid4().hex,
            'expires_at':record['expires_at']}
