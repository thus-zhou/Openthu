"""Consistent read-only precheck objects for library and sports sources.

The adapters already return source-shaped rows.  This module turns those rows
into one additive object that keeps five questions separate:

* which resource was selected and whether the selector was a stable id or a
  display name that needs the user to choose among candidates;
* for which window the read applies;
* what the source currently reports as free (and over which scope);
* whether *this account* may proceed (almost always unverified by a read);
* which fees and rules the source actually declared, with their unit and the
  time they were read.

Nothing here performs I/O.  Missing facts stay ``UNKNOWN``.  A free seat is
never promoted to bookable, an area total is never reported as slot
availability, and an unrecognised unit is never converted by guessing.
"""
from __future__ import annotations

from . import cache_policy, domain_models, source_state

VERSION = 'library-sports-precheck-v1'
TIMEZONE_NAME = 'Asia/Shanghai'
UNITS = ('MINUTE', 'FEN', 'PERSON', 'COUNT', 'FLAG', 'TEXT')
INT_UNITS = {'MINUTE', 'FEN', 'PERSON', 'COUNT'}

# Platform rule fields and the unit the source declares for them.  A field not
# listed here is not converted.  ``None`` means the source field is known but
# its unit is ambiguous (for example a numeric ``cancelTime`` that may be a
# clock value or a number of minutes); such a value is retained as UNKNOWN
# rather than interpreted.
ROOM_RULE_UNITS = {
    'ruleName': 'TEXT', 'memo': 'TEXT',
    'earliestResvTime': 'MINUTE', 'latestResvTime': 'MINUTE',
    'minResvTime': 'MINUTE', 'maxResvTime': 'MINUTE',
    'cancelTime': 'MINUTE', 'timeInterval': 'MINUTE',
    'resvBeforeNoticeTime': 'MINUTE', 'resvAfterNoticeTime': 'MINUTE',
    'resvEndNewTime': 'MINUTE', 'earlyInTime': 'MINUTE',
    'limit': 'COUNT',
}
SPORTS_RULE_UNITS = {
    'cancelBeforeStart': 'MINUTE',
    'cancelTime': None,
    'deadlineTime': None,
}

# Stable service-level source ids for UIDs.  Operation names are deliberately
# not used: the same record must have the same UID in search, detail and
# precheck output.
SOURCE_IDS = {
    'catalog': 'library.catalog',
    'spaces': 'library.spaces',
    'loans': 'library.loans',
    'sports': 'sports.venue',
    'orders': 'sports.orders',
}
LIVE_OPERATIONS = {'seat_availability', 'room_availability', 'availability', 'order_detail'}
HISTORY_OPERATIONS = {'loans', 'reservations', 'orders'}
READ_ONLY_OPERATIONS = {'catalog_search', 'catalog_detail', 'database_links',
                        'scenes', 'room_types'}


def _checked(value):
    """Return a normalized check time or None; never silently say "now"."""
    parsed = domain_models.canonical_time(value)
    if parsed['valid'] and parsed['date_precision'] != 'date':
        return parsed['value']
    return None


def _source(url):
    return source_state.sanitize_url(url) if url else None


def _identity(source_id, kind, record_id):
    result = domain_models.identity(source_id, kind, record_id)
    return {'uid': result['uid'], 'identity_state': result['identity_state']}


def money(value, *, raw_field, source=None, checked_at=None):
    """Fees are integer fen in the source; display never multiplies by 100."""
    item = {'amount_fen': None, 'display': None, 'currency': 'CNY', 'unit': 'FEN',
            'state': 'UNKNOWN', 'conversion': 'EXACT_INTEGER_DIVIDE_BY_100',
            'raw_field': raw_field, 'source': _source(source), 'checked_at': checked_at}
    if type(value) is int and value >= 0:
        item.update(amount_fen=value, display=f'{value // 100}.{value % 100:02d}',
                    state='QUOTED')
    return item


def rule_value(value, unit, *, raw_field, source=None, checked_at=None, note=''):
    """Keep a declared rule with its unit instead of inventing a default."""
    item = {'raw_field': raw_field, 'value': None,
            'unit': unit if unit in UNITS else 'UNKNOWN', 'state': 'MISSING',
            'source': _source(source), 'checked_at': checked_at, 'note': note}
    if value is None:
        return item
    if unit not in UNITS:
        item['state'] = 'UNIT_UNREGISTERED'
        return item
    if unit in INT_UNITS:
        valid = type(value) is int and value >= 0
    elif unit == 'FLAG':
        valid = isinstance(value, (bool, int)) and value in (True, False, 0, 1)
        if valid:
            value = bool(value)
    else:
        valid = isinstance(value, str) and bool(value.strip())
        if valid:
            value = value.strip()[:500]
    if not valid:
        item['state'] = 'TYPE_MISMATCH'
        return item
    item.update(value=value, state='DECLARED')
    return item


def _rules(mapping, raw, *, source, checked_at):
    items = []
    raw = raw or {}
    for field, unit in mapping.items():
        if unit is None:
            if field in raw and raw[field] is not None:
                entry = rule_value(None, 'UNKNOWN', raw_field=field, source=source,
                                   checked_at=checked_at)
                entry.update(state='UNIT_AMBIGUOUS', note='unit not registered for this field')
            else:
                entry = rule_value(None, 'UNKNOWN', raw_field=field, source=source,
                                   checked_at=checked_at)
            items.append(entry)
            continue
        items.append(rule_value(raw.get(field), unit, raw_field=field,
                                source=source, checked_at=checked_at))
    declared = sum(item['state'] == 'DECLARED' for item in items)
    unusual = any(item['state'] in ('UNIT_AMBIGUOUS', 'UNIT_UNREGISTERED', 'TYPE_MISMATCH')
                  for item in items)
    if items and declared == len(items):
        state = 'READ'
    elif declared or unusual:
        state = 'PARTIAL'
    else:
        state = 'MISSING'
    return {'state': state, 'items': items}


def _text_rules(values, *, raw_field='rules', source=None, checked_at=None):
    rows = [value for value in (values or []) if isinstance(value, str) and value.strip()]
    items = [rule_value(value, 'TEXT', raw_field=raw_field, source=source, checked_at=checked_at)
             for value in rows]
    return {'state': 'READ' if items else 'MISSING', 'items': items}


def _fees_unknown(reason):
    return {'state': 'UNKNOWN', 'items': [], 'reason': reason}


def _candidate(row):
    keys = ('area_id', 'kind_id', 'room_id', 'scene_id', 'database_id', 'name', 'path',
            'capacity', 'class_kind', 'min_people', 'max_people')
    return {key: row[key] for key in keys if key in row}


def _selection(raw, parameters, *, selected=False, limit=20):
    candidates = [_candidate(row) for row in (raw.get('candidates') or [])]
    total = len(candidates)
    shown = candidates[:limit]
    by = 'NONE'
    if any(parameters.get(key) for key in
           ('area_id', 'kind_id', 'room_id', 'scene_id', 'record_id', 'database_id', 'parent_id')):
        by = 'ID'
    elif parameters.get('name'):
        by = 'NAME'
    if selected or raw.get('resource'):
        state = 'UNIQUE'
    elif raw.get('status') == 'NOT_FOUND':
        state = 'NOT_FOUND'
    elif raw.get('status') == 'NEEDS_USER_ACTION' or shown:
        state = 'AMBIGUOUS' if shown else 'NEEDS_USER_ACTION'
    else:
        state = 'ALL'
    return {'state': state, 'by': by, 'candidate_count': total,
            'candidate_total': total, 'candidates_truncated': total > limit,
            'candidates': shown}


def _window(date_value=None, start=None, end=None):
    if start and end:
        precision = 'MINUTE'
    elif date_value:
        precision = 'DATE'
    else:
        precision = 'UNKNOWN'
    return {'date': date_value, 'start_time': start, 'end_time': end,
            'timezone': TIMEZONE_NAME, 'precision': precision}


def _base(operation, checked_at):
    return {'version': VERSION, 'operation': operation, 'checked_at': checked_at,
            'selection': {'state': 'NOT_APPLICABLE', 'by': 'NONE',
                          'candidate_count': 0, 'candidate_total': 0,
                          'candidates_truncated': False, 'candidates': []},
            'resource': None,
            'window': _window(),
            'availability': {'state': 'NOT_APPLICABLE', 'free_count': None,
                             'capacity': None, 'capacity_scope': None,
                             'capacity_is_slot_availability': False,
                             'source_complete': None, 'reason': None},
            'eligibility': {'state': 'NOT_APPLICABLE', 'reasons': []},
            'fees': {'state': 'NOT_APPLICABLE', 'items': [], 'reason': None},
            'rules': {'state': 'NOT_APPLICABLE', 'items': []},
            'unknowns': [],
            'preparation_state': 'NOT_APPLICABLE'}


def _seat_availability(raw, items, parameters, checked_at):
    block = _base('seat_availability', checked_at)
    resource = raw.get('resource') or {}
    free = raw.get('free_count')
    capacity = resource.get('capacity')
    block['selection'] = _selection(raw, parameters)
    block['resource'] = {
        'resource_id': resource.get('area_id'),
        'name': resource.get('name'),
        'path': resource.get('path'),
        **_identity(SOURCE_IDS['spaces'], 'RESOURCE', resource.get('area_id')),
        'capacity': ({'value': capacity, 'unit': 'COUNT', 'scope': 'AREA_TOTAL_SEATS',
                      'source_field': 'seatinfo.TotalCount', 'is_slot_availability': False}
                     if type(capacity) is int and capacity >= 0 else None),
    }
    block['window'] = _window(raw.get('date'), raw.get('start_time'), raw.get('end_time'))
    complete = raw.get('complete') is True
    mapped = sum(1 for item in items if item.get('state_mapping') == 'MAPPED')
    unknown = len(items) - mapped
    if raw.get('status') in ('NOT_OPEN', 'NOT_FOUND', 'NEEDS_USER_ACTION', 'BROWSER_REQUIRED'):
        state = 'UNKNOWN'
    elif not items or type(free) is not int:
        state = 'UNKNOWN'
    elif not complete:
        state = 'PARTIAL'
    elif unknown:
        state = 'PARTIAL'
    elif free > 0:
        state = 'FREE'
    else:
        state = 'FULL'
    block['availability'] = {
        'state': state, 'free_count': free if type(free) is int else None,
        'capacity': capacity if type(capacity) is int else None,
        'capacity_scope': 'AREA_TOTAL_SEATS', 'capacity_is_slot_availability': False,
        'source_complete': complete, 'reason': raw.get('reason') or
        ('NO_SEAT_ROWS' if not items else 'UNKNOWN_SEAT_STATUS' if unknown else None),
        'row_count': len(items), 'mapped_seat_count': mapped, 'unknown_seat_count': unknown,
        'slot': ({'segment_id': raw.get('segment_id'), 'start_time': raw.get('start_time'),
                  'end_time': raw.get('end_time')} if raw.get('segment_id') else None),
    }
    block['eligibility'] = {'state': 'UNVERIFIED', 'reasons': ['ACCOUNT_NOT_CHECKED']}
    block['fees'] = _fees_unknown('SEAT_FEE_STATE_NOT_REPORTED')
    block['rules'] = _text_rules(raw.get('rules'), raw_field='ska.bookRule',
                                 source=raw.get('rule_source'), checked_at=checked_at)
    block['unknowns'] = ['ACCOUNT_ELIGIBILITY', 'FEE_STATE',
                         'CANCELLATION_AND_NO_SHOW_TERMS']
    if not items:
        block['unknowns'].append('SEAT_MAP_ROWS')
    if unknown:
        block['unknowns'].append('SEAT_STATUS_MAPPING')
    if raw.get('status') in ('NEEDS_USER_ACTION', 'NOT_FOUND'):
        block['preparation_state'] = 'NEEDS_USER_ACTION'
    elif raw.get('status') == 'BROWSER_REQUIRED':
        block['preparation_state'] = 'NEEDS_OFFICIAL_PAGE'
    elif raw.get('status') == 'NOT_OPEN':
        block['preparation_state'] = 'NOT_PREPARABLE'
    elif state == 'FREE':
        block['preparation_state'] = 'NEEDS_ELIGIBILITY_CHECK'
    elif state == 'FULL':
        block['preparation_state'] = 'NOT_PREPARABLE'
    else:
        block['preparation_state'] = 'UNKNOWN'
    return block


def _seat_item(item, raw, checked_at):
    block = _base('seat_availability', checked_at)
    mapped = item.get('state_mapping') == 'MAPPED'
    free = item.get('free') if mapped else None
    block['resource'] = {
        'resource_id': item.get('seat_id'), 'name': item.get('name'),
        'area_id': item.get('area_id'),
        **_identity(SOURCE_IDS['spaces'], 'RESOURCE', item.get('seat_id')),
        'source_status_digest': item.get('source_status_digest'),
    }
    block['window'] = _window(raw.get('date'), raw.get('start_time'), raw.get('end_time'))
    block['availability'] = {
        'state': 'FREE' if free is True else 'FULL' if free is False else 'UNKNOWN',
        'free_count': 1 if free is True else 0 if free is False else None,
        'capacity': 1 if mapped else None, 'capacity_scope': 'SEAT_UNIT',
        'capacity_is_slot_availability': True, 'source_complete': None,
        'reason': None if mapped else 'UNKNOWN_SEAT_STATUS',
    }
    block['eligibility'] = {'state': 'UNVERIFIED', 'reasons': ['ACCOUNT_NOT_CHECKED']}
    block['fees'] = _fees_unknown('SEAT_FEE_STATE_NOT_REPORTED')
    block['rules'] = {'state': 'MISSING', 'items': []}
    block['unknowns'] = ['ACCOUNT_ELIGIBILITY', 'FEE_STATE']
    if not mapped:
        block['unknowns'].append('SEAT_STATUS_MAPPING')
    block['preparation_state'] = ('NEEDS_ELIGIBILITY_CHECK' if free is True
                                  else 'NOT_PREPARABLE' if free is False else 'UNKNOWN')
    return block


def _room_item(item, restrictions, raw, checked_at):
    block = _base('room_availability', checked_at)
    block['resource'] = {
        'resource_id': item.get('room_id'), 'kind_id': item.get('kind_id'),
        'name': item.get('name'), 'room_name': item.get('room_name'),
        'location': item.get('location'),
        **_identity(SOURCE_IDS['spaces'], 'RESOURCE', item.get('room_id')),
        'people_limit': {'min': item.get('min_people'), 'max': item.get('max_people'),
                         'unit': 'PERSON', 'source_fields': ['minUser', 'maxUser']},
    }
    block['window'] = _window(raw.get('date'), raw.get('start_time'), raw.get('end_time'))
    interval = item.get('interval_state')
    availability_state = {'FREE_TIME_ONLY': 'FREE', 'BUSY': 'BUSY', 'NOT_OPEN': 'NOT_OPEN',
                          'NOT_SELECTED': 'NOT_SELECTED',
                          'DURATION_OUT_OF_RANGE': 'RULE_MISMATCH',
                          'RULE_UNVERIFIED': 'RULE_UNVERIFIED'}.get(interval, 'UNKNOWN')
    block['availability'] = {
        'state': availability_state, 'free_count': None, 'capacity': None,
        'capacity_scope': 'ROOM_INTERVAL', 'capacity_is_slot_availability': False,
        'source_complete': None, 'reason': interval,
        'opening_intervals': item.get('opening_intervals') or [],
        'occupied_intervals': item.get('occupied_intervals') or [],
    }
    reasons = []
    if item.get('party_state') in ('PARTY_TOO_SMALL', 'PARTY_TOO_LARGE'):
        reasons.append(item['party_state'])
    if item.get('only_view') is True:
        reasons.append('VIEW_ONLY_RESOURCE')
    if restrictions:
        reasons.append('ACCOUNT_RESTRICTIONS_REPORTED')
    if reasons:
        eligibility = 'INELIGIBLE'
    elif item.get('party_state') in ('IN_RANGE', 'UNSPECIFIED'):
        eligibility = 'UNVERIFIED'
        reasons.append('ACCOUNT_NOT_CHECKED')
    else:
        eligibility = 'UNKNOWN'
    block['eligibility'] = {'state': eligibility, 'reasons': reasons}
    block['fees'] = _fees_unknown('ROOM_FEE_STATE_NOT_REPORTED')
    block['rules'] = _rules(ROOM_RULE_UNITS, item.get('rules') or {},
                            source=item.get('rule_source') or raw.get('rule_source'),
                            checked_at=checked_at)
    block['unknowns'] = ['ACCOUNT_ELIGIBILITY', 'FEE_STATE',
                         'APPROVAL_OR_OFFICIAL_FORM_REQUIREMENT']
    if eligibility == 'INELIGIBLE':
        block['preparation_state'] = 'NOT_PREPARABLE'
    elif availability_state in ('BUSY', 'NOT_OPEN', 'RULE_MISMATCH'):
        block['preparation_state'] = 'NOT_PREPARABLE'
    elif availability_state == 'NOT_SELECTED':
        block['preparation_state'] = 'NEEDS_USER_ACTION'
    elif availability_state == 'RULE_UNVERIFIED':
        block['preparation_state'] = 'NEEDS_RULE_CHECK'
    elif availability_state == 'FREE':
        block['preparation_state'] = 'NEEDS_ELIGIBILITY_CHECK'
    else:
        block['preparation_state'] = 'UNKNOWN'
    return block


def public_occupancy(rows):
    """Only time and platform state leave the source for other people's rows."""
    allowed = ('start_time', 'end_time', 'platform_status')
    items = [{key: row.get(key) for key in allowed}
             for row in (rows or []) if isinstance(row, dict)]
    return {'state': 'REDACTED', 'items': items,
            'removed': ['identity', 'subject', 'notes'],
            'basis': 'PUBLIC_PREVIEW_TIME_AND_PLATFORM_STATUS_ONLY'}


def _room_availability(raw, items, parameters, checked_at):
    block = _base('room_availability', checked_at)
    block['selection'] = _selection(raw, parameters)
    resource = raw.get('resource') or {}
    block['resource'] = {
        'resource_id': resource.get('kind_id'), 'name': resource.get('name'),
        'kind_id': resource.get('kind_id'),
        **_identity(SOURCE_IDS['spaces'], 'RESOURCE', resource.get('kind_id')),
    }
    block['window'] = _window(raw.get('date'), raw.get('start_time'), raw.get('end_time'))
    restrictions = (raw.get('account_restrictions') or {}).get('active_records')
    block['availability'] = {
        'state': 'LISTED' if items else 'UNKNOWN', 'free_count': None, 'capacity': None,
        'capacity_scope': 'ROOM_LIST', 'capacity_is_slot_availability': False,
        'source_complete': raw.get('complete') is True, 'reason': None,
        'rooms_read': len(items),
    }
    block['eligibility'] = {
        'state': 'INELIGIBLE' if type(restrictions) is int and restrictions > 0 else 'UNVERIFIED',
        'reasons': (['ACCOUNT_RESTRICTIONS_REPORTED'] if type(restrictions) is int and restrictions > 0
                    else ['ACCOUNT_NOT_CHECKED'])}
    block['fees'] = _fees_unknown('ROOM_FEE_STATE_NOT_REPORTED')
    block['rules'] = {'state': 'PER_ROOM', 'items': []}
    block['unknowns'] = ['ACCOUNT_ELIGIBILITY', 'FEE_STATE',
                         'APPROVAL_OR_OFFICIAL_FORM_REQUIREMENT']
    if raw.get('status') in ('NEEDS_USER_ACTION', 'NOT_FOUND'):
        block['preparation_state'] = 'NEEDS_USER_ACTION'
    elif not items:
        block['preparation_state'] = 'UNKNOWN'
    else:
        block['preparation_state'] = 'SEE_ITEMS'
    return block


def _sports_site(raw, items, parameters, checked_at):
    block = _base('availability', checked_at)
    selected = bool(raw.get('scene') and raw.get('room') and not raw.get('candidates'))
    block['selection'] = _selection(raw, parameters, selected=selected)
    if parameters.get('scene_id') or parameters.get('room_id'):
        block['selection']['by'] = 'ID'
    elif parameters.get('name'):
        block['selection']['by'] = 'NAME'
    scene = raw.get('scene') or {}
    room = raw.get('room') or {}
    block['resource'] = {
        'resource_id': room.get('room_id'), 'name': room.get('name'),
        'scene_id': scene.get('scene_id'), 'scene_name': scene.get('name'),
        'room_id': room.get('room_id'),
        **_identity(SOURCE_IDS['sports'], 'RESOURCE', room.get('room_id')),
    }
    block['window'] = _window(raw.get('date'))
    sessions = [session for item in items for session in (item.get('sessions') or [])]
    states = [session.get('available') for session in sessions]
    if not sessions:
        state = 'UNKNOWN'
    elif any(value is True for value in states):
        state = 'FREE'
    elif all(value is False for value in states):
        state = 'FULL'
    else:
        state = 'UNKNOWN'
    block['availability'] = {
        'state': state, 'free_count': sum(value is True for value in states),
        'capacity': None, 'capacity_scope': 'SITE_SESSION_LIST_NOT_VENUE_CAPACITY',
        'capacity_is_slot_availability': False,
        'source_complete': raw.get('complete') is True,
        'reason': 'NO_VERIFIED_SESSION' if not sessions else None,
        'site_count': len(items), 'session_count': len(sessions),
        'available_session_count': sum(value is True for value in states),
    }
    block['eligibility'] = {'state': 'UNVERIFIED', 'reasons': ['ACCOUNT_NOT_CHECKED']}
    fees = []
    for item in items:
        for session in item.get('sessions') or []:
            fees.append(money(session.get('unit_fee_fen'),
                              raw_field='sessionVo.userFeeDetails.chargingUnitPrice',
                              source=raw.get('url'), checked_at=checked_at))
    block['fees'] = ({'state': 'QUOTED' if any(f['state'] == 'QUOTED' for f in fees) else 'UNKNOWN',
                      'items': fees, 'reason': None if fees else 'SESSION_FEE_NOT_REPORTED'})
    block['rules'] = {'state': 'PER_SESSION', 'items': []}
    block['unknowns'] = ['ACCOUNT_ELIGIBILITY', 'PAYMENT_AND_REFUND_TERMS']
    if raw.get('status') in ('NEEDS_USER_ACTION', 'NOT_FOUND'):
        block['preparation_state'] = 'NEEDS_USER_ACTION'
    elif not sessions:
        block['preparation_state'] = 'UNKNOWN'
    elif state == 'FREE':
        block['preparation_state'] = 'NEEDS_ELIGIBILITY_CHECK'
    elif state == 'FULL':
        block['preparation_state'] = 'NOT_PREPARABLE'
    else:
        block['preparation_state'] = 'UNKNOWN'
    return block


def _sports_site_item(item, raw, checked_at):
    block = _base('availability', checked_at)
    block['window'] = _window(raw.get('date'))
    block['resource'] = {
        'resource_id': item.get('site_id'), 'name': item.get('name'),
        'scene_id': item.get('scene_id'), 'room_id': item.get('room_id'),
        **_identity(SOURCE_IDS['sports'], 'RESOURCE', item.get('site_id')),
    }
    sessions = item.get('sessions') or []
    states = [session.get('available') for session in sessions]
    state = 'FREE' if any(value is True for value in states) else 'FULL' if sessions and all(
        value is False for value in states) else 'UNKNOWN'
    block['availability'] = {
        'state': state, 'free_count': sum(value is True for value in states),
        'capacity': None, 'capacity_scope': 'SITE_SESSION_LIST_NOT_VENUE_CAPACITY',
        'capacity_is_slot_availability': False, 'source_complete': raw.get('complete') is True,
        'reason': 'NO_VERIFIED_SESSION' if not sessions else None,
        'session_count': len(sessions), 'available_session_count': sum(value is True for value in states),
    }
    block['eligibility'] = {'state': 'UNVERIFIED', 'reasons': ['ACCOUNT_NOT_CHECKED']}
    block['fees'] = {'state': 'SEE_SESSIONS', 'items': [], 'reason': None}
    block['rules'] = _rules(SPORTS_RULE_UNITS, item.get('cancel_rule') or {},
                            source=raw.get('url'), checked_at=checked_at)
    block['unknowns'] = ['ACCOUNT_ELIGIBILITY', 'PAYMENT_AND_REFUND_TERMS']
    block['preparation_state'] = ('NEEDS_ELIGIBILITY_CHECK' if state == 'FREE'
                                  else 'NOT_PREPARABLE' if state == 'FULL' else 'UNKNOWN')
    return block


def _sports_session(session, site, raw, checked_at):
    block = _base('availability', checked_at)
    capacity = session.get('capacity') if type(session.get('capacity')) is int else None
    block['resource'] = {
        'resource_id': session.get('session_id'), 'site_id': site.get('site_id'),
        'scene_id': site.get('scene_id'), 'room_id': site.get('room_id'),
        **_identity(SOURCE_IDS['sports'], 'RESOURCE', session.get('session_id')),
        'capacity': ({'value': capacity, 'unit': 'PERSON',
                      'scope': 'SESSION_ALLOW_USER_NUM',
                      'source_field': 'sessionVo.allowUserNum',
                      'is_slot_availability': True}
                     if capacity is not None else None),
        'reserved_count': session.get('reserved_count'),
    }
    block['window'] = _window((session.get('start_at') or '')[:10],
                              (session.get('start_at') or '')[11:16],
                              (session.get('end_at') or '')[11:16])
    available = session.get('available')
    block['availability'] = {
        'state': 'FREE' if available is True else 'FULL' if available is False else 'UNKNOWN',
        'free_count': None, 'capacity': capacity,
        'capacity_scope': 'SESSION_ALLOW_USER_NUM', 'capacity_is_slot_availability': True,
        'source_complete': raw.get('complete') is True,
        'reason': session.get('reason') if available is None else None,
    }
    block['eligibility'] = {'state': 'UNVERIFIED', 'reasons': ['ACCOUNT_NOT_CHECKED']}
    fees = [money(session.get('unit_fee_fen'),
                  raw_field='sessionVo.userFeeDetails.chargingUnitPrice',
                  source=raw.get('url'), checked_at=checked_at),
            money(session.get('quoted_total_fen'),
                  raw_field='sessionVo.userFeeDetails.payableAmount',
                  source=raw.get('url'), checked_at=checked_at)]
    block['fees'] = {'state': 'QUOTED' if any(f['state'] == 'QUOTED' for f in fees) else 'UNKNOWN',
                     'items': fees, 'reason': None}
    block['rules'] = _rules(SPORTS_RULE_UNITS, site.get('cancel_rule') or {},
                            source=raw.get('url'), checked_at=checked_at)
    block['unknowns'] = ['ACCOUNT_ELIGIBILITY', 'PAYMENT_AND_REFUND_TERMS']
    if available is False:
        block['preparation_state'] = 'NOT_PREPARABLE'
    elif available is True:
        block['preparation_state'] = 'NEEDS_ELIGIBILITY_CHECK'
    else:
        block['preparation_state'] = 'UNKNOWN'
    return block


def _order_block(item, raw, checked_at, operation):
    block = _base(operation, checked_at)
    block['resource'] = {
        'resource_id': item.get('reservation_id'), 'name': item.get('scene_name'),
        'scene_id': item.get('scene_id'),
        **_identity(SOURCE_IDS['orders'], 'ORDER', item.get('reservation_id')),
    }
    block['window'] = _window((item.get('start_at') or '')[:10],
                              (item.get('start_at') or '')[11:16],
                              (item.get('end_at') or '')[11:16])
    block['availability'] = {'state': 'NOT_APPLICABLE', 'free_count': None,
                             'capacity': None, 'capacity_scope': None,
                             'capacity_is_slot_availability': False,
                             'source_complete': raw.get('complete') is True, 'reason': None}
    block['eligibility'] = {'state': 'NOT_APPLICABLE', 'reasons': []}
    fees = [money(item.get('paid_amount_fen'), raw_field='paidAmount',
                  source=item.get('official_url'), checked_at=checked_at),
            money(item.get('total_amount_fen'), raw_field='totalAmount',
                  source=item.get('official_url'), checked_at=checked_at)]
    block['fees'] = {'state': 'QUOTED' if any(f['state'] == 'QUOTED' for f in fees) else 'UNKNOWN',
                     'items': fees, 'reason': None}
    block['rules'] = _rules(SPORTS_RULE_UNITS,
                            {'cancelBeforeStart': item.get('cancel_before_start_minutes')},
                            source=item.get('official_url'), checked_at=checked_at)
    block['unknowns'] = ['PAYMENT_RECEIPT', 'REFUND_SETTLEMENT']
    block['preparation_state'] = 'NOT_APPLICABLE'
    block['status_axes'] = item.get('status_axes')
    block['state_confirmed'] = item.get('state_confirmed')
    return block


def _loan_item(item, raw, checked_at):
    block = _base('loans', checked_at)
    block['resource'] = {
        'resource_id': item.get('loan_id'), 'name': item.get('title'),
        **_identity(SOURCE_IDS['loans'], 'RENEWAL', item.get('loan_id')),
    }
    block['window'] = _window((item.get('due_at') or '')[:10] or item.get('due_date'))
    state = {'ELIGIBLE': 'ELIGIBLE', 'NOT_ELIGIBLE': 'NOT_ELIGIBLE'}.get(
        item.get('renewal_state'), 'UNKNOWN')
    block['eligibility'] = {
        'state': state,
        'reasons': item.get('renewal_hints') or ['RENEWAL_HINT_ONLY'],
        'renewal': {'state': 'HINT_ONLY', 'source_field': 'renew',
                    'does_not_change_due_date': True, 'execution_attempted': False},
    }
    block['fees'] = _fees_unknown('OVERDUE_FEE_STATE_NOT_REPORTED')
    renew_flag = {'Y': True, 'N': False}.get(item.get('renew_flag'))
    rules = [rule_value(renew_flag, 'FLAG', raw_field='renew',
                        source=raw.get('url'), checked_at=checked_at)]
    rules.extend(rule_value(value, 'TEXT', raw_field='renewstatuses.renewstatus',
                            source=raw.get('url'), checked_at=checked_at)
                 for value in (item.get('renewal_hints') or []))
    block['rules'] = {'state': 'READ' if any(r['state'] == 'DECLARED' for r in rules)
                      else 'MISSING', 'items': rules}
    block['unknowns'] = ['RENEWAL_EXECUTION', 'OVERDUE_FEE_STATE']
    return block


def _copy_item(copy, holding, book, checked_at):
    block = _base('catalog_detail', checked_at)
    state = copy.get('borrowable_state') or ('NOT_BORROWABLE' if copy.get('borrowable') is False
                                             else 'UNVERIFIED')
    block['resource'] = {
        'resource_id': copy.get('item_id'), 'name': book.get('title'),
        'holding_id': holding.get('holding_id'), 'record_id': book.get('record_id'),
        **_identity(SOURCE_IDS['catalog'], 'RESOURCE', copy.get('item_id')),
    }
    block['availability'] = {
        'state': 'NOT_BORROWABLE' if state == 'NOT_BORROWABLE' else 'COPY_VISIBLE',
        'free_count': None, 'capacity': None, 'capacity_scope': 'COPY_STATUS',
        'capacity_is_slot_availability': False, 'source_complete': None,
        'reason': copy.get('status') if state == 'NOT_BORROWABLE' else None,
    }
    block['eligibility'] = {'state': 'UNVERIFIED', 'reasons': ['ACCOUNT_NOT_CHECKED']}
    block['fees'] = _fees_unknown('LIBRARY_FEE_STATE_NOT_REPORTED')
    block['rules'] = _text_rules([copy.get('loan_policy')], raw_field='item_data.policy',
                                 source=book.get('detail_url'), checked_at=checked_at)
    block['unknowns'] = ['ACCOUNT_BORROW_ELIGIBILITY', 'FEE_STATE']
    block['preparation_state'] = ('NOT_PREPARABLE' if state == 'NOT_BORROWABLE'
                                  else 'NEEDS_ELIGIBILITY_CHECK')
    return block


def build(service, operation, raw, items, parameters, checked_at):
    """Return ``{'summary': block, 'items': [block, ...]}`` for one read op."""
    checked = _checked(checked_at)
    parameters = parameters or {}
    items = items or []
    per_item = []
    if service == 'library' and operation == 'seat_availability':
        summary = _seat_availability(raw, items, parameters, checked)
        per_item = [_seat_item(item, raw, checked) for item in items]
        for item, block in zip(items, per_item):
            item['precheck'] = block
    elif service == 'library' and operation == 'room_availability':
        summary = _room_availability(raw, items, parameters, checked)
        restrictions = (raw.get('account_restrictions') or {}).get('active_records')
        per_item = [_room_item(item, type(restrictions) is int and restrictions > 0,
                               raw, checked) for item in items]
        for item, block in zip(items, per_item):
            item['precheck'] = block
    elif service == 'sports' and operation == 'availability':
        summary = _sports_site(raw, items, parameters, checked)
        for item in items:
            site_block = _sports_site_item(item, raw, checked)
            item['precheck'] = site_block
            for session in item.get('sessions') or []:
                session['precheck'] = _sports_session(session, item, raw, checked)
            per_item.append(site_block)
    elif service == 'sports' and operation in ('orders', 'order_detail'):
        for item in items:
            block = _order_block(item, raw, checked, operation)
            item['precheck'] = block
            per_item.append(block)
        summary = _order_summary(raw, items, parameters, checked, operation)
    elif service == 'library' and operation == 'loans':
        for item in items:
            block = _loan_item(item, raw, checked)
            item['precheck'] = block
            per_item.append(block)
        summary = _collection_summary('loans', checked, raw, parameters,
                                      ['RENEWAL_EXECUTION', 'OVERDUE_FEE_STATE'])
    elif service == 'library' and operation == 'reservations':
        summary = _collection_summary('reservations', checked, raw, parameters,
                                      ['LIVE_CANCELLATION_RULE'])
    elif service == 'library' and operation == 'catalog_detail':
        summary = _catalog_summary(raw, items, parameters, checked)
        for book in items:
            for holding in book.get('holdings') or []:
                for copy in holding.get('copies') or []:
                    block = _copy_item(copy, holding, book, checked)
                    copy['precheck'] = block
                    per_item.append(block)
    elif service == 'library' and operation == 'catalog_search':
        summary = _catalog_search_summary(raw, parameters, checked)
        for item in items:
            block = _base('catalog_search', checked)
            block['resource'] = {'resource_id': item.get('record_id'), 'name': item.get('title'),
                                 **_identity(SOURCE_IDS['catalog'], 'RESOURCE',
                                             item.get('record_id'))}
            block['availability'] = {'state': 'UNKNOWN', 'free_count': None, 'capacity': None,
                                     'capacity_scope': 'SEARCH_RESULT_NOT_HOLDINGS',
                                     'capacity_is_slot_availability': False,
                                     'source_complete': None,
                                     'reason': 'HOLDINGS_NOT_READ'}
            block['eligibility'] = {'state': 'UNVERIFIED', 'reasons': ['ACCOUNT_NOT_CHECKED']}
            block['fees'] = _fees_unknown('LIBRARY_FEE_STATE_NOT_REPORTED')
            block['unknowns'] = ['HOLDINGS_AVAILABILITY', 'ACCOUNT_BORROW_ELIGIBILITY', 'FEE_STATE']
            block['preparation_state'] = 'UNKNOWN'
            item['precheck'] = block
            per_item.append(block)
    elif service == 'library' and operation == 'database_links':
        summary = _collection_summary('database_links', checked, raw, parameters,
                                      ['INSTITUTIONAL_AUTHORIZATION', 'OFF_CAMPUS_ACCESS'])
        if parameters.get('database_id') and len(items) == 1:
            summary['selection'] = {**summary['selection'], 'state': 'UNIQUE', 'by': 'ID'}
        summary['electronic_access'] = {
            'state': 'OFFICIAL_GUIDE_ONLY', 'institutional_authorization': 'UNVERIFIED',
            'off_campus_access': 'UNVERIFIED', 'third_party_terms': 'NOT_CHECKED',
            'cookie_forwarded': False}
    elif service == 'sports' and operation == 'scenes':
        summary = _collection_summary('scenes', checked, raw, parameters,
                                      ['SESSION_AVAILABILITY', 'ACCOUNT_ELIGIBILITY'])
    elif service == 'library' and operation == 'room_types':
        summary = _collection_summary('room_types', checked, raw, parameters,
                                      ['ROOM_AVAILABILITY', 'ACCOUNT_ELIGIBILITY'])
    else:
        summary = _collection_summary(operation, checked, raw, parameters, [])
    return {'summary': summary, 'items': per_item}


def _catalog_summary(raw, items, parameters, checked):
    summary = _collection_summary('catalog_detail', checked, raw, parameters,
                                  ['ACCOUNT_BORROW_ELIGIBILITY', 'FEE_STATE',
                                   'OFF_CAMPUS_AUTHORIZATION'])
    summary['selection'] = _selection(raw, parameters,
                                      selected=bool(parameters.get('record_id') and items))
    summary['kind'] = 'PHYSICAL_AND_ELECTRONIC_SEPARATE'
    summary['electronic_access'] = {
        'state': 'LISTED' if any(book.get('electronic_links') for book in items)
                 or any(book.get('electronic_access_page') for book in items) else 'NOT_REPORTED',
        'institutional_authorization': 'UNVERIFIED',
        'off_campus_access': 'UNVERIFIED', 'third_party_terms': 'NOT_CHECKED',
        'cookie_forwarded': False}
    copies = [copy for book in items for holding in (book.get('holdings') or [])
              for copy in (holding.get('copies') or [])]
    if not copies:
        summary['availability'] = {'state': 'NO_PHYSICAL_COPY_REPORTED', 'free_count': None,
                                   'capacity': None, 'capacity_scope': 'HOLDINGS',
                                   'capacity_is_slot_availability': False,
                                   'source_complete': raw.get('complete') is True,
                                   'reason': 'PHYSICAL_HOLDINGS_NOT_REPORTED'}
        summary['preparation_state'] = 'UNKNOWN'
    else:
        visible = any(copy.get('borrowable') is not False for copy in copies)
        summary['availability'] = {'state': 'COPY_VISIBLE' if visible else 'NOT_BORROWABLE',
                                   'free_count': None, 'capacity': None,
                                   'capacity_scope': 'COPY_STATUS',
                                   'capacity_is_slot_availability': False,
                                   'source_complete': raw.get('complete') is True,
                                   'reason': None if visible else 'ALL_COPIES_NOT_BORROWABLE'}
        summary['preparation_state'] = ('NEEDS_ELIGIBILITY_CHECK' if visible
                                        else 'NOT_PREPARABLE')
    summary['resource'] = ({'resource_id': items[0].get('record_id'),
                            'name': items[0].get('title'),
                            **_identity(SOURCE_IDS['catalog'], 'RESOURCE',
                                        items[0].get('record_id'))} if items else None)
    return summary


def _catalog_search_summary(raw, parameters, checked):
    summary = _collection_summary('catalog_search', checked, raw, parameters,
                                  ['HOLDINGS_AVAILABILITY', 'ACCOUNT_BORROW_ELIGIBILITY',
                                   'FEE_STATE'])
    summary['selection'] = _selection(raw, parameters)
    summary['availability'] = {'state': 'NOT_APPLICABLE', 'free_count': None, 'capacity': None,
                               'capacity_scope': 'SEARCH_RESULT_NOT_HOLDINGS',
                               'capacity_is_slot_availability': False,
                               'source_complete': raw.get('complete') is True,
                               'reason': 'HOLDINGS_NOT_READ'}
    return summary


def _order_summary(raw, items, parameters, checked, operation):
    summary = _collection_summary(operation, checked, raw, parameters,
                                  ['PAYMENT_RECEIPT', 'REFUND_SETTLEMENT'])
    summary['selection'] = _selection(raw, parameters,
                                      selected=bool(parameters.get('record_id') and items))
    axes = [item.get('status_axes') or {} for item in items]
    unknown = [axis for row in axes for axis, value in row.items()
               if isinstance(value, dict) and value.get('mapping_state') != 'MAPPED']
    if unknown:
        summary['unknowns'] = sorted(set(summary['unknowns']) | {axis.upper() + '_STATUS'
                                                                 for axis in unknown})
    summary['status_axes_state'] = 'PARTIAL' if unknown else 'READ'
    summary['status_axes_unknown'] = sorted({axis for row in axes for axis, value in row.items()
                                             if isinstance(value, dict)
                                             and value.get('mapping_state') != 'MAPPED'})
    return summary


def _collection_summary(operation, checked, raw, parameters, unknowns):
    summary = _base(operation, checked)
    summary['selection'] = _selection(raw, parameters)
    summary['unknowns'] = list(unknowns)
    summary['availability'] = {'state': 'NOT_APPLICABLE', 'free_count': None, 'capacity': None,
                               'capacity_scope': None, 'capacity_is_slot_availability': False,
                               'source_complete': raw.get('complete') is True, 'reason': None}
    summary['rules'] = {'state': 'NOT_APPLICABLE', 'items': []}
    return summary


def _snapshot_version():
    # Import lazily so precheck has no module-import cycle with queries.
    try:
        from .queries import SNAPSHOT_VERSION
        return SNAPSHOT_VERSION
    except Exception:
        return 'snapshot-v2'


def retrieval(service, operation, *, checked_at, parameters=None, reused_snapshot=False):
    """Declare whether this result is a fresh source read or an old snapshot."""
    parameters = parameters or {}
    static_entry = (operation == 'database_links' and not parameters.get('query')
                    and not parameters.get('database_id'))
    if operation == 'order_detail':
        policy_label = 'LIVE_ONLY'
    elif operation in READ_ONLY_OPERATIONS:
        policy_label = 'STATIC_ENTRY_LIST' if static_entry else 'LIVE_READ_ONLY'
    elif operation in LIVE_OPERATIONS:
        policy = cache_policy.policy(f'{service}.{operation}') or cache_policy.policy('availability')
        policy_label = policy['label'] if policy else 'LIVE_ONLY'
    elif operation in HISTORY_OPERATIONS:
        policy = cache_policy.policy(f'{service}.{operation}') or cache_policy.policy('history')
        policy_label = policy['label'] if policy else 'HISTORY_FALLBACK_ALLOWED'
    else:
        policy_label = 'NOT_REGISTERED'
    if reused_snapshot:
        freshness = 'SNAPSHOT_ONLY_NOT_LIVE'
    elif static_entry:
        freshness = 'STATIC_ENTRY_LIST'
    else:
        freshness = 'LIVE_AT_CHECKED_AT'
    return {'policy': policy_label,
            'freshness': freshness,
            'current_source_read': not reused_snapshot and not static_entry,
            'reused_snapshot': reused_snapshot, 'checked_at': _checked(checked_at),
            'snapshot_version': _snapshot_version(), 'reusable_for_booking': False,
            'proves_authentication': False}
