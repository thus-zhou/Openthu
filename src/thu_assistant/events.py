"""Unified, traceable agenda over registered read results.

Module 5.4.  This module turns the existing todo aggregation and the academics
``calendar-event-v1`` conversion into one schedule model:

* every item keeps a stable identifier derived from account, source and source
  record -- never from a title or array position;
* a date-only value stays ``time.kind=DATE`` and is never padded to midnight;
* deadlines (assignment/loan), reservation intervals and recurring course
  occurrences keep separate time semantics instead of sharing one due field;
* an unfamiliar business status can only become ``UNKNOWN``;
* an item without any usable date is kept as an explicitly undated agenda item
  (``calendar_valid=False``) instead of receiving a fabricated date;
* two same-title records from different sources keep separate identities;
* time conflicts are computed only for intervals whose start and end come from
  a trusted source or an explicit period table.  All-day values, unknown times
  and timetable occurrences without a period table never participate.

No function here performs I/O, reads credentials or contacts a campus service.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import re

from . import domain_models, source_state
from .bridge import CampusError

VERSION = 'agenda-events-v1'
ACCOUNT_DOMAIN = 'CAMPUS'
ACCOUNT_DOMAINS = {'CAMPUS', 'MODEL', 'LOCAL', 'SYSTEM', 'UNKNOWN'}
SERVICE_DOMAINS = {'INFO', 'LEARN', 'LIBRARY', 'SPORTS', 'MADMODEL', 'LOCAL', 'UNKNOWN'}
EVENT_KINDS = {'COURSE', 'ASSIGNMENT', 'EXAM', 'LIBRARY_LOAN', 'SPACE_RESERVATION',
               'SPORTS_ORDER', 'CUSTOM'}
EXPORT_STATES = {'NOT_EXPORTED', 'EXPORTED', 'STALE', 'UNKNOWN'}
TZ = timezone(timedelta(hours=8))

_PART = re.compile(r'^[A-Za-z0-9_.:+-]{1,240}$')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# ``time_semantics`` separates a deadline from an interval and from a
# rule-derived occurrence.  A single ``due_at`` field cannot represent all
# three without inventing meaning.
SOURCE_SPECS = {
    'learn.assignments': {
        'service_domain': 'LEARN', 'kind': 'ASSIGNMENT', 'time_semantics': 'DEADLINE',
        'version': 'learn-assignment-event-v1',
    },
    'library.loans': {
        'service_domain': 'LIBRARY', 'kind': 'LIBRARY_LOAN', 'time_semantics': 'DEADLINE',
        'version': 'library-loan-event-v1',
    },
    'library.seat': {
        'service_domain': 'LIBRARY', 'kind': 'SPACE_RESERVATION', 'time_semantics': 'INTERVAL',
        'version': 'library-seat-event-v1',
    },
    'library.room': {
        'service_domain': 'LIBRARY', 'kind': 'SPACE_RESERVATION', 'time_semantics': 'INTERVAL',
        'version': 'library-room-event-v1',
    },
    'sports.orders': {
        'service_domain': 'SPORTS', 'kind': 'SPORTS_ORDER', 'time_semantics': 'INTERVAL',
        'version': 'sports-order-event-v1',
    },
    'academics.exams': {
        'service_domain': 'UNKNOWN', 'kind': 'EXAM', 'time_semantics': 'OCCURRENCE',
        'version': 'academics-event-v1',
    },
    'academics.timetable': {
        'service_domain': 'UNKNOWN', 'kind': 'COURSE', 'time_semantics': 'OCCURRENCE',
        'version': 'academics-event-v1',
    },
}

BUSINESS_STATUS = {
    'TODO': 'CONFIRMED', 'BORROWED': 'CONFIRMED', 'SCHEDULED': 'CONFIRMED',
    'IN_PROGRESS': 'CONFIRMED', 'ATTENDED': 'CONFIRMED', 'COMPLETED': 'CONFIRMED',
    'SUBMITTED': 'CONFIRMED', 'GRADED': 'CONFIRMED', 'NO_SHOW': 'CONFIRMED',
    'APPROVED': 'CONFIRMED', 'PENDING_PAYMENT': 'TENTATIVE',
    'PENDING_APPROVAL': 'TENTATIVE', 'CANCELLED': 'CANCELLED',
    'REJECTED': 'CANCELLED', 'UNKNOWN': 'UNKNOWN',
}
TERMINAL = {'SUBMITTED', 'GRADED', 'COMPLETED', 'ATTENDED', 'CANCELLED', 'REJECTED', 'NO_SHOW'}
CALENDAR_STATUSES = {'CONFIRMED', 'TENTATIVE', 'CANCELLED', 'UNKNOWN'}


def clean_text(value, limit=500):
    """Strip control characters, keep line breaks and cap the length."""
    if value is None:
        return ''
    text = str(value).replace('\r\n', '\n').replace('\r', '\n')
    return _CONTROL.sub('', text).strip()[:limit]


def _timestamp(value, fallback=None):
    """Normalize an observation time without inventing midnight."""
    return source_state.normalize_timestamp(value, fallback) or fallback


def _precision(value):
    if not isinstance(value, str):
        return 'unknown'
    if _DATE.fullmatch(value):
        return 'date'
    if re.search(r'\.\d+', value):
        return 'millisecond'
    if re.search(r'T\d{2}:\d{2}:\d{2}', value) or re.search(r' \d{2}:\d{2}:\d{2}', value):
        return 'second'
    if re.search(r'[T ]\d{2}:\d{2}', value):
        return 'minute'
    return 'unknown'


def _safe_sequence(value):
    """Return ``(sequence, invalid)`` without a native ValueError escape."""
    if value is None or value == '':
        return 0, False
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0, True
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0, True
    return max(0, number), False


def _valid_date(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_datetime(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})', value):
        return False
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return False
    return True


def _date_time(value, end=None):
    """Return ``(time_object, warning)``; an invalid range degrades to DATE."""
    try:
        time_value = domain_models.calendar_time(value, end)
    except domain_models.RecordError as error:
        if error.code == 'END_BEFORE_START' and value and _DATE.match(str(value)[:10]):
            return ({'kind': 'DATE', 'date': str(value)[:10],
                     'timezone': domain_models.TIMEZONE_NAME},
                    'TIME_RANGE_INVALID_DATE_ONLY')
        return None, 'TIME_INVALID'
    if time_value is None:
        return None, 'TIME_INVALID'
    return time_value, None


def _record_id(event_id):
    if isinstance(event_id, str) and _PART.fullmatch(event_id):
        return event_id
    return None


def _event_uid(service_domain, kind, event_id):
    record_id = _record_id(event_id)
    if record_id is None:
        return None
    return domain_models.event_uid(ACCOUNT_DOMAIN, service_domain, kind, record_id, '0')


def _calendar_status(business_status):
    status = BUSINESS_STATUS.get(business_status, 'UNKNOWN')
    warnings = []
    if business_status == 'REJECTED':
        warnings.append('REJECTED_MAPPED_TO_CANCELLED')
    if status == 'UNKNOWN' and business_status != 'UNKNOWN':
        warnings.append('STATUS_UNKNOWN')
    return status, warnings


def _completeness(status):
    status = status if isinstance(status, dict) else {}
    return {
        'source_complete': status.get('complete') is True,
        'snapshot_complete': status.get('snapshot_complete') is not False,
        'complete': status.get('complete') is True,
    }


def from_todo(row, *, source_status=None):
    """Convert one legacy ``get_todos`` row to an agenda item.

    ``calendar_valid`` is false when the row has no usable date or identifier;
    the item is still returned so the undated view is honest.
    """
    if not isinstance(row, dict):
        raise CampusError('EVENT_INVALID_RECORD')
    source = row.get('source')
    spec = SOURCE_SPECS.get(source)
    if spec is None:
        raise CampusError('EVENT_UNKNOWN_SOURCE')
    event_id = row.get('event_id')
    if _record_id(event_id) is None:
        raise CampusError('EVENT_INVALID_RECORD')
    title = clean_text(row.get('title')) or '未命名事项'
    checked_at = _timestamp(row.get('checked_at'), source_state.now_iso())
    time_value, time_warning = None, None
    if row.get('start_at') or row.get('due_at'):
        time_value, time_warning = _date_time(row.get('start_at') or row.get('due_at'),
                                              row.get('end_at'))
    precision = row.get('date_precision') or 'unknown'
    if time_value is not None and time_value.get('kind') == 'DATE':
        precision = 'date'
    elif time_value is not None:
        precision = _precision(time_value.get('start'))
    business_status = str(row.get('status') or 'UNKNOWN')
    status, status_warnings = _calendar_status(business_status)
    uid = _event_uid(spec['service_domain'], spec['kind'], event_id) if time_value is not None else None
    sequence, bad_sequence = _safe_sequence(row.get('sequence'))
    warnings = sorted(set(row.get('warnings') or ()) | set(status_warnings))
    if bad_sequence:
        warnings = sorted(set(warnings) | {'SEQUENCE_INVALID'})
    if time_warning:
        warnings = sorted(set(warnings) | {time_warning})
    source_id = row.get('source_id')
    trusted = (spec['time_semantics'] == 'INTERVAL'
               and isinstance(time_value, dict)
               and time_value.get('kind') == 'DATE_TIME'
               and time_value.get('end') is not None)
    return {
        'event_id': event_id,
        'event_uid': uid,
        'account_domain': ACCOUNT_DOMAIN,
        'source': {
            'service_domain': spec['service_domain'],
            'kind': spec['kind'],
            'record_id': event_id,
            'stable_resource_uid': event_id,
        },
        'source_id': source,
        'source_version': spec['version'],
        'title': title,
        'time': time_value,
        'time_precision': precision if time_value is not None else 'unknown',
        'time_semantics': spec['time_semantics'],
        'status': status,
        'business_status': business_status,
        'source_status': row.get('source_status'),
        'sequence': sequence,
        'last_modified': checked_at,
        'source_evidence_id': source,
        'location': clean_text(row.get('location'), 300) or None,
        'description': None,
        'ics_uid': None,
        'export': {'state': 'NOT_EXPORTED', 'exported_at': None},
        'source_url': source_state.sanitize_url(row.get('source_url')),
        'checked_at': checked_at,
        'freshness': row.get('freshness') or 'LIVE_AT_CHECKED_AT',
        'completeness': _completeness(source_status),
        'calendar_valid': bool(uid and time_value is not None),
        'time_trusted': bool(trusted),
        'warnings': warnings,
        'derivation': {'kind': 'NORMALISED', 'from': [source], 'not_a_source_fact': False},
        'legacy_source_id': source_id,
    }


def _validated_calendar_event(event):
    """Validate a ``calendar-event-v1`` object without trusting the caller."""
    if not isinstance(event, dict):
        raise CampusError('EVENT_INVALID_RECORD')
    uid = event.get('event_uid')
    account = event.get('account_domain')
    source = event.get('source')
    time_value = event.get('time')
    status = event.get('status')
    title = clean_text(event.get('title'))
    if (not isinstance(uid, str) or not _PART.fullmatch(uid)
            or account not in ACCOUNT_DOMAINS
            or not isinstance(source, dict)
            or source.get('service_domain') not in SERVICE_DOMAINS
            or source.get('kind') not in EVENT_KINDS
            or not isinstance(source.get('record_id'), str)
            or not _PART.fullmatch(source.get('record_id'))
            or not isinstance(source.get('stable_resource_uid'), str)
            or not _PART.fullmatch(source.get('stable_resource_uid'))
            or not title or len(title) > 500
            or status not in CALENDAR_STATUSES):
        raise CampusError('EVENT_INVALID_RECORD')
    if not isinstance(time_value, dict):
        raise CampusError('EVENT_INVALID_TIME')
    if time_value.get('kind') == 'DATE':
        if not _valid_date(time_value.get('date')):
            raise CampusError('EVENT_INVALID_TIME')
    elif time_value.get('kind') == 'DATE_TIME':
        if not _valid_datetime(time_value.get('start')):
            raise CampusError('EVENT_INVALID_TIME')
        if time_value.get('end') is not None:
            if not _valid_datetime(time_value.get('end')):
                raise CampusError('EVENT_INVALID_TIME')
            start = datetime.fromisoformat(str(time_value['start']).replace('Z', '+00:00'))
            end = datetime.fromisoformat(str(time_value['end']).replace('Z', '+00:00'))
            if not start.tzinfo:
                start = start.replace(tzinfo=TZ)
            if not end.tzinfo:
                end = end.replace(tzinfo=TZ)
            if end < start:
                raise CampusError('EVENT_INVALID_TIME')
    else:
        raise CampusError('EVENT_INVALID_TIME')
    if not isinstance(time_value.get('timezone'), str) or not 1 <= len(time_value['timezone']) <= 80:
        raise CampusError('EVENT_INVALID_TIME')
    last_modified = source_state.normalize_timestamp(event.get('last_modified'))
    if last_modified is None:
        raise CampusError('EVENT_INVALID_RECORD')
    evidence = event.get('source_evidence_id')
    if not isinstance(evidence, str) or not 1 <= len(evidence) <= 120:
        raise CampusError('EVENT_INVALID_RECORD')
    ics_uid = event.get('ics_uid')
    if ics_uid is not None and (not isinstance(ics_uid, str) or len(ics_uid) > 240
                                or _CONTROL.search(ics_uid)):
        raise CampusError('EVENT_INVALID_RECORD')
    location = event.get('location')
    description = event.get('description')
    if location is not None and (not isinstance(location, str) or len(location) > 300):
        raise CampusError('EVENT_INVALID_RECORD')
    if description is not None and (not isinstance(description, str) or len(description) > 4000):
        raise CampusError('EVENT_INVALID_RECORD')
    export = event.get('export')
    if export is not None and (not isinstance(export, dict)
                               or export.get('state') not in EXPORT_STATES):
        raise CampusError('EVENT_INVALID_RECORD')
    sequence, bad_sequence = _safe_sequence(event.get('sequence'))
    return {
        'event_uid': uid, 'account_domain': account, 'source': deepcopy(source),
        'title': title, 'time': deepcopy(time_value), 'status': status,
        'sequence': sequence, 'sequence_invalid': bad_sequence,
        'last_modified': last_modified, 'source_evidence_id': evidence,
        'location': clean_text(location, 300) or None,
        'description': clean_text(description, 4000) or None,
        'ics_uid': ics_uid,
        'export': deepcopy(export) if export is not None
                  else {'state': 'NOT_EXPORTED', 'exported_at': None},
    }


def from_calendar_event(event, *, source_id, source_status=None):
    """Wrap a validated ``calendar-event-v1`` object as an agenda item."""
    checked = _validated_calendar_event(event)
    warnings = set(clean_text(item, 80) for item in event.get('warnings') or ()
                   if isinstance(item, str) and clean_text(item, 80))
    if checked['sequence_invalid']:
        warnings.add('SEQUENCE_INVALID')
    time_value = checked['time']
    derived = event.get('derivation') if isinstance(event.get('derivation'), dict) else {}
    precision = 'date' if time_value.get('kind') == 'DATE' else _precision(time_value.get('start'))
    trusted = (time_value.get('kind') == 'DATE_TIME' and time_value.get('end') is not None
               and 'TIME_NOT_FROM_PERIOD_TABLE' not in warnings)
    item = deepcopy(event)
    item.update({
        'event_id': checked['event_uid'],
        'event_uid': checked['event_uid'],
        'account_domain': checked['account_domain'],
        'source': checked['source'],
        'source_id': source_id,
        'source_version': derived.get('rule_version') or SOURCE_SPECS.get(
            source_id, {}).get('version', 'calendar-event-v1'),
        'title': checked['title'],
        'time': time_value,
        'time_precision': precision,
        'time_semantics': 'OCCURRENCE',
        'business_status': checked['status'],
        'source_status': None,
        'sequence': checked['sequence'],
        'last_modified': checked['last_modified'],
        'source_evidence_id': checked['source_evidence_id'],
        'location': checked['location'],
        'description': checked['description'],
        'ics_uid': checked['ics_uid'],
        'export': checked['export'],
        'source_url': source_state.sanitize_url(event.get('source_url')),
        'checked_at': _timestamp(event.get('checked_at'), checked['last_modified']),
        'freshness': event.get('freshness') or 'LIVE_AT_CHECKED_AT',
        'completeness': _completeness(source_status),
        'calendar_valid': True,
        'time_trusted': bool(trusted),
        'warnings': sorted(warnings),
        'derivation': derived or {'kind': 'DERIVED', 'not_a_source_fact': True},
    })
    return item


def to_calendar_event(item):
    """Return only the frozen ``calendar-event-v1`` fields.

    The agenda item adds presentation and evidence fields; export and contract
    checks consume this strict subset so extra fields cannot leak into the
    frozen shape by accident.
    """
    if not isinstance(item, dict) or item.get('calendar_valid') is not True:
        raise CampusError('EVENT_INVALID_RECORD')
    checked = _validated_calendar_event({
        'event_uid': item.get('event_uid'),
        'account_domain': item.get('account_domain', ACCOUNT_DOMAIN),
        'source': item.get('source'),
        'title': item.get('title'),
        'time': item.get('time'),
        'status': item.get('status'),
        'sequence': item.get('sequence'),
        'last_modified': _timestamp(item.get('last_modified'), item.get('checked_at')),
        'source_evidence_id': item.get('source_evidence_id'),
        'location': item.get('location'),
        'description': item.get('description'),
        'ics_uid': item.get('ics_uid'),
        'export': item.get('export'),
    })
    return {
        'event_uid': checked['event_uid'],
        'account_domain': checked['account_domain'],
        'source': deepcopy(checked['source']),
        'title': checked['title'],
        'time': deepcopy(checked['time']),
        'status': checked['status'],
        'sequence': checked['sequence'],
        'last_modified': checked['last_modified'],
        'source_evidence_id': checked['source_evidence_id'],
        'location': checked['location'],
        'description': checked['description'],
        'ics_uid': checked['ics_uid'],
        'export': deepcopy(checked['export']),
    }


def _sort_key(item):
    time_value = item.get('time') or {}
    if time_value.get('kind') == 'DATE':
        return (str(time_value.get('date') or '') + 'T00:00', item.get('event_id') or '')
    if time_value.get('kind') == 'DATE_TIME':
        return (str(time_value.get('start') or ''), item.get('event_id') or '')
    return ('9999-12-31T99', item.get('event_id') or '')


def _point(item):
    """Return a comparable datetime for a deadline/occurrence, if known."""
    time_value = item.get('time') or {}
    if time_value.get('kind') == 'DATE':
        try:
            return datetime.fromisoformat(str(time_value.get('date')) + 'T00:00:00').replace(tzinfo=TZ), 'date'
        except (ValueError, TypeError):
            return None, 'unknown'
    if time_value.get('kind') == 'DATE_TIME' and time_value.get('start'):
        try:
            value = datetime.fromisoformat(str(time_value['start']).replace('Z', '+00:00'))
        except ValueError:
            return None, 'unknown'
        return (value if value.tzinfo else value.replace(tzinfo=TZ)).astimezone(TZ), 'date-time'
    return None, 'unknown'


def _interval(item):
    time_value = item.get('time') or {}
    if time_value.get('kind') != 'DATE_TIME' or not time_value.get('end'):
        return None
    try:
        start = datetime.fromisoformat(str(time_value['start']).replace('Z', '+00:00'))
        end = datetime.fromisoformat(str(time_value['end']).replace('Z', '+00:00'))
    except ValueError:
        return None
    if not start.tzinfo:
        start = start.replace(tzinfo=TZ)
    if not end.tzinfo:
        end = end.replace(tzinfo=TZ)
    start, end = start.astimezone(TZ), end.astimezone(TZ)
    return (start, end) if start < end else None


def _is_overdue(item, now):
    if item.get('business_status') not in ('TODO', 'BORROWED'):
        return None
    point, precision = _point(item)
    if point is None:
        return None
    return point.date() < now.date() if precision == 'date' else point < now


def build_views(items, *, now, window=None):
    """Build the required views over one query instant.

    Counts and event ids are returned, not a second copy of every object.
    """
    window = window if isinstance(window, dict) else {}
    start = _as_date(window.get('start_date'))
    end = _as_date(window.get('end_date'))
    upcoming, overdue, undated, completed = [], [], [], []
    by_source = {}
    for item in items:
        source = item.get('source_id')
        by_source.setdefault(source, []).append(item)
        if item.get('calendar_valid') is not True:
            undated.append(item)
            continue
        business = item.get('business_status')
        if business in TERMINAL or item.get('status') == 'CANCELLED':
            completed.append(item)
            continue
        if _is_overdue(item, now):
            overdue.append(item)
            continue
        interval = _interval(item)
        if interval is not None:
            # A reservation that starts on the last window day and ends after
            # midnight still belongs to the window.  Compare date spans.
            if interval[1] < now:
                continue
            if start is not None and interval[1].date() < start:
                continue
            if end is not None and interval[0].date() > end:
                continue
            upcoming.append(item)
            continue
        point, precision = _point(item)
        if point is None:
            undated.append(item)
            continue
        day = point.date()
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            continue
        if precision != 'date' and point < now and day < now.date():
            continue
        upcoming.append(item)
    upcoming.sort(key=_sort_key)
    overdue.sort(key=_sort_key)
    undated.sort(key=_sort_key)
    return {
        'as_of': now.isoformat(timespec='seconds'),
        'window': {'start_date': start.isoformat() if start else None,
                   'end_date': end.isoformat() if end else None,
                   'inclusive': True},
        'views': {
            'upcoming': {'count': len(upcoming),
                         'event_ids': [item['event_id'] for item in upcoming]},
            'overdue': {'count': len(overdue),
                        'event_ids': [item['event_id'] for item in overdue]},
            'undated': {'count': len(undated),
                        'event_ids': [item['event_id'] for item in undated]},
            'completed': {'count': len(completed),
                          'event_ids': [item['event_id'] for item in completed]},
            'by_source': {
                source: {'count': len(group),
                         'event_ids': [item['event_id'] for item in sorted(group, key=_sort_key)]}
                for source, group in sorted(by_source.items())
            },
        },
    }


def conflicts(items, *, limit=100):
    """Return intersecting trusted intervals only.

    All-day items, deadline points, unknown times and timetable occurrences
    without an explicit period table never participate.
    """
    candidates = []
    for item in items:
        if (item.get('time_trusted') is not True
                or item.get('status') in ('CANCELLED', 'UNKNOWN')
                or item.get('business_status') in TERMINAL | {'CANCELLED', 'REJECTED'}):
            continue
        interval = _interval(item)
        if interval is None:
            continue
        candidates.append((item, interval))
    candidates.sort(key=lambda pair: (pair[1][0], pair[0].get('event_uid') or ''))
    found = []
    for index, (left, left_span) in enumerate(candidates):
        for right, right_span in candidates[index + 1:]:
            if left_span[0] >= right_span[1]:
                break
            if right_span[0] >= left_span[1]:
                continue
            pair = sorted([left.get('event_uid') or left['event_id'],
                           right.get('event_uid') or right['event_id']])
            found.append({
                'basis': 'TRUSTED_INTERVAL_OVERLAP',
                'events': pair,
                'start': max(left_span[0], right_span[0]).isoformat(timespec='seconds'),
                'end': min(left_span[1], right_span[1]).isoformat(timespec='seconds'),
                'sources': sorted({left.get('source_id'), right.get('source_id')}),
            })
            if len(found) >= limit:
                return found
    return found


def _as_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def in_window(item, start, end, *, include_completed=False, include_overdue=True):
    """Whether an agenda item belongs to the requested date window.

    Used for academic events, which do not pass through the legacy todo
    ``select()``.  Intervals intersect by date; date-only and point events use
    their own day.  Terminal rows follow the same opt-in as legacy todos.
    """
    if not isinstance(item, dict):
        return False
    time_value = item.get('time') if isinstance(item.get('time'), dict) else {}
    if time_value.get('kind') not in ('DATE', 'DATE_TIME'):
        return False
    first = _as_date(start)
    last = _as_date(end)
    if first is None or last is None:
        return True
    status = item.get('status')
    business = item.get('business_status')
    if status == 'CANCELLED' or business in TERMINAL | {'CANCELLED', 'REJECTED'}:
        if not include_completed:
            return False
    interval = _interval(item)
    if interval is not None:
        return interval[1].date() >= first and interval[0].date() <= last
    point, _ = _point(item)
    if point is None:
        return False
    return first <= point.date() <= last


def build_agenda(todo_items, academic_events=(), *, statuses=(), now=None, window=None,
                 checked_at=None):
    """Build agenda items, the undated bucket, views, conflicts and warnings."""
    now = now or datetime.now(TZ)
    if isinstance(now, str):
        parsed = _timestamp(now)
        now = datetime.fromisoformat(parsed) if parsed else datetime.now(TZ)
    status_map = {row.get('source'): row for row in statuses or () if isinstance(row, dict)}
    items, excluded, warnings = [], [], set()
    for row in todo_items or ():
        source = row.get('source') if isinstance(row, dict) else None
        event_id = row.get('event_id') if isinstance(row, dict) else None
        try:
            item = from_todo(row, source_status=status_map.get(source))
        except CampusError as error:
            excluded.append({'source': source, 'event_id': event_id,
                             'reason': error.code})
            warnings.add(error.code)
            continue
        items.append(item)
        warnings |= set(item['warnings'])
    for source_id, event in academic_events or ():
        try:
            item = from_calendar_event(event, source_id=source_id,
                                       source_status=status_map.get(source_id))
        except CampusError as error:
            excluded.append({'source': source_id,
                             'event_id': event.get('event_uid') if isinstance(event, dict) else None,
                             'reason': error.code})
            warnings.add(error.code)
            continue
        items.append(item)
        warnings |= set(item['warnings'])
    unique, duplicate = {}, set()
    for item in items:
        key = item.get('event_uid') or item.get('event_id')
        if key in unique:
            duplicate.add(key)
            warnings.add('EVENT_UID_CONFLICT')
            continue
        unique[key] = item
    items = sorted(unique.values(), key=_sort_key)
    calendar_events = [item for item in items if item.get('calendar_valid') is True]
    undated = [item for item in items if item.get('calendar_valid') is not True]
    selected_window = window if isinstance(window, dict) else {}
    views = build_views(items, now=now, window=selected_window)
    return {
        'items': items,
        'calendar_events': calendar_events,
        'undated': undated,
        'excluded': excluded,
        'warnings': sorted(warnings),
        'views': views['views'],
        'as_of': views['as_of'],
        'window': views['window'],
        'conflicts': conflicts(items),
        'checked_at': _timestamp(checked_at, source_state.now_iso()),
    }


class AcademicBridge:
    """Read the academics capability table and convert available records.

    The default production table is ``BLOCKED_SOURCE_EVIDENCE``; ``read`` then
    raises ``ACADEMICS_SOURCE_BLOCKED`` and the caller records an explicit
    status instead of inventing exams or a timetable.  Tests can inject the
    same protocol with synthetic events.
    """

    def capabilities(self):
        from .academics.reader import capability_report
        return capability_report()['capabilities']

    def read(self, capability, *, semester=None, expansions=None):
        from .academics.reader import AcademicsReader
        from .academics import events as academic_events
        result = AcademicsReader().read(capability, semester=semester)
        if result.get('implementation_state') == 'BLOCKED':
            raise CampusError('ACADEMICS_SOURCE_BLOCKED')
        converted = academic_events.calendar_events(capability, result.get('items') or (),
                                                    expansions=expansions)
        complete = bool(result.get('complete')) and not converted['excluded']
        scope = result.get('read_scope') if isinstance(result.get('read_scope'), dict) else {}
        pages_read = scope.get('pages_read')
        if type(pages_read) is not int or pages_read < 0:
            pages_read = 1
        return {
            'events': converted['events'],
            'excluded': converted['excluded'],
            'warnings': sorted(set(converted['warnings']) | set(result.get('warnings') or ())),
            'complete': complete,
            'checked_at': result.get('checked_at'),
            'source_version': academic_events.VERSION,
            'pages_read': pages_read,
            'records_read': len(result.get('items') or ()),
            'semester': semester,
        }


__all__ = [
    'ACCOUNT_DOMAIN', 'ACCOUNT_DOMAINS', 'AcademicBridge', 'BUSINESS_STATUS',
    'CALENDAR_STATUSES', 'EVENT_KINDS', 'EXPORT_STATES', 'SERVICE_DOMAINS',
    'SOURCE_SPECS', 'TERMINAL', 'VERSION', 'build_agenda', 'build_views',
    'clean_text', 'conflicts', 'from_calendar_event', 'from_todo',
    'in_window', 'to_calendar_event',
]
