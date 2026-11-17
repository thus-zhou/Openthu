"""Calendar-event conversion for timetable occurrences and exams (4.1).

Consumers such as the calendar/agenda module (5.4) read ``calendar-event-v1``
objects, not raw academics records.  This module performs that conversion and
nothing else:

* a date-only exam stays ``time.kind=DATE``; no clock time and no midnight;
* an event status is derived from the source state and release state with an
  explicit rule version, and an unfamiliar state can only become ``UNKNOWN``;
* course grades are never converted into events or todos;
* an entry without a stable identifier or a date produces no event and is
  reported in ``excluded`` instead of receiving a fabricated UID.
"""
from __future__ import annotations

from .. import domain_models
from .errors import AcademicsError

VERSION = 'academics-event-v1'
EXAM_RULE = 'exam-event-v1'
TIMETABLE_RULE = 'timetable-event-v1'
SERVICE_DOMAIN = 'UNKNOWN'
ACCOUNT_DOMAIN = 'CAMPUS'

EXAM_EVENT_STATUS = {
    'SCHEDULED': 'TENTATIVE',
    'MAKEUP': 'TENTATIVE',
    'DEFERRED': 'TENTATIVE',
    'RESCHEDULED': 'TENTATIVE',
    'CANCELLED': 'CANCELLED',
    'UNKNOWN': 'UNKNOWN',
}


def _record_id(record):
    source = str(record.get('source_id') or '')
    value = str(record.get('source_record_id') or '')
    combined = (source.replace('.', '_') + '_' + value).strip('_')
    return combined if value and len(combined) <= 240 else None


def _event_kind(record):
    return 'COURSE' if record.get('kind') == 'TIMETABLE_ENTRY' else record.get('kind')


def _event_uid(record, occurrence):
    record_id = _record_id(record)
    if record_id is None:
        return None
    return domain_models.event_uid(ACCOUNT_DOMAIN, SERVICE_DOMAIN, _event_kind(record),
                                   record_id, occurrence)


def _base(record, *, title, time_value, status, sequence, derivation, warnings):
    record_id = _record_id(record)
    if record_id is None or not title:
        return None
    uid = _event_uid(record, '0' if record['kind'] == 'EXAM' else derivation['occurrence'])
    if uid is None:
        return None
    return {
        'event_uid': uid,
        'account_domain': ACCOUNT_DOMAIN,
        'source': {
            'service_domain': SERVICE_DOMAIN,
            'kind': _event_kind(record),
            'record_id': record_id,
            'stable_resource_uid': record.get('exam_uid') or record.get('entry_uid') or record_id,
        },
        'title': title,
        'time': time_value,
        'status': status,
        'sequence': sequence,
        'last_modified': record.get('observed_at') or record.get('checked_at'),
        'source_evidence_id': record.get('source_evidence_id'),
        'location': record.get('location'),
        'description': None,
        'ics_uid': None,
        'export': {'state': 'NOT_EXPORTED', 'exported_at': None},
        'derivation': derivation,
        'warnings': sorted(set(record.get('warnings') or ()) | set(warnings or ())),
    }


def exam_event(exam, *, sequence=0):
    """Convert one normalised exam record to ``calendar-event-v1``."""
    if not isinstance(exam, dict) or exam.get('kind') != 'EXAM':
        raise AcademicsError('ACADEMICS_EVENT_UNSUPPORTED', 'exam')
    if not exam.get('date'):
        raise AcademicsError('ACADEMICS_EVENT_UNSUPPORTED', 'date')
    if not exam.get('exam_uid'):
        raise AcademicsError('ACADEMICS_EVENT_UNSUPPORTED', 'identity')
    title = str(exam.get('title') or '')[:500]
    if exam.get('time_precision') == 'minute' and exam.get('start_time'):
        start = f"{exam['date']}T{exam['start_time']}:00"
        end = f"{exam['date']}T{exam['end_time']}:00" if exam.get('end_time') else None
        time_value = domain_models.calendar_time(start, end)
    else:
        time_value = {'kind': 'DATE', 'date': exam['date'],
                      'timezone': domain_models.TIMEZONE_NAME}
    state = exam.get('state') if exam.get('state') in EXAM_EVENT_STATUS else 'UNKNOWN'
    status = EXAM_EVENT_STATUS[state]
    if state == 'SCHEDULED' and exam.get('release_state') == 'PUBLISHED':
        status = 'CONFIRMED'
    derivation = {
        'kind': 'DERIVED',
        'rule_version': EXAM_RULE,
        'from': ['exam.state', 'exam.release_state'],
        'not_a_source_fact': True,
        'occurrence': '0',
    }
    warnings = []
    if state == 'UNKNOWN':
        warnings.append('EXAM_STATE_UNKNOWN_FOR_EVENT')
    event = _base(exam, title=title, time_value=time_value, status=status,
                  sequence=max(0, int(sequence or 0)), derivation=derivation,
                  warnings=warnings)
    if event is None:
        raise AcademicsError('ACADEMICS_EVENT_UNSUPPORTED', 'identity')
    return event


def timetable_events(entry, expansion, *, sequence=0):
    """Convert expanded timetable occurrences; returns an event list."""
    if not isinstance(entry, dict) or entry.get('kind') != 'TIMETABLE_ENTRY':
        raise AcademicsError('ACADEMICS_EVENT_UNSUPPORTED', 'timetable_entry')
    if not isinstance(expansion, dict) or expansion.get('state') not in ('EXPANDED', 'EXPANDED_DATE_ONLY'):
        return {'events': [], 'excluded': [{'reason': 'NOT_EXPANDED',
                                            'state': (expansion or {}).get('state')}],
                'warnings': []}
    title = str(entry.get('title') or '')[:500]
    events, excluded = [], []
    for occurrence in expansion.get('occurrences') or ():
        derivation = {
            'kind': 'DERIVED',
            'rule_version': TIMETABLE_RULE,
            'from': ['week_pattern', 'school_calendar', 'period_table'],
            'expansion_rule': (expansion.get('derivation') or {}).get('rule_version'),
            'occurrence': str(occurrence.get('date')),
            'not_a_source_fact': True,
        }
        time_value = occurrence.get('time')
        if time_value is None:
            time_value = {'kind': 'DATE', 'date': occurrence.get('date'),
                          'timezone': domain_models.TIMEZONE_NAME}
        event = _base(entry, title=title, time_value=time_value, status='TENTATIVE',
                      sequence=max(0, int(sequence or 0)), derivation=derivation,
                      warnings=[] if occurrence.get('time_state') == 'PERIOD_TABLE'
                      else ['TIME_NOT_FROM_PERIOD_TABLE'])
        if event is None:
            excluded.append({'reason': 'MISSING_STABLE_ID_OR_TITLE',
                             'date': occurrence.get('date')})
        else:
            events.append(event)
    return {'events': events, 'excluded': excluded,
            'warnings': sorted({warning for event in events for warning in event['warnings']})}


def calendar_events(capability, records, *, expansions=None, sequence=0):
    """Convert a capability result; grades are refused on purpose."""
    if capability == 'grades':
        raise AcademicsError('ACADEMICS_GRADE_NOT_EVENT')
    rows = [row for row in records or () if isinstance(row, dict)]
    events, excluded, warnings = [], [], set()
    if capability == 'exams':
        for row in rows:
            try:
                events.append(exam_event(row, sequence=sequence))
            except AcademicsError as error:
                excluded.append({'reason': error.code,
                                 'source_record_id': row.get('source_record_id')})
                warnings |= set(row.get('warnings') or ())
    elif capability == 'timetable':
        mapping = expansions
        if isinstance(mapping, (list, tuple)):
            mapping = {row.get('entry_uid'): expansion
                       for row, expansion in zip(rows, mapping)}
        mapping = mapping or {}
        for row in rows:
            expansion = mapping.get(row.get('entry_uid'))
            if expansion is None:
                excluded.append({'reason': 'NOT_EXPANDED',
                                 'source_record_id': row.get('source_record_id')})
                continue
            converted = timetable_events(row, expansion, sequence=sequence)
            events.extend(converted['events'])
            excluded.extend(converted['excluded'])
            warnings |= set(converted['warnings'])
    else:
        raise AcademicsError('ACADEMICS_UNKNOWN_CAPABILITY', str(capability))
    return {'events': events, 'excluded': excluded, 'warnings': sorted(warnings),
            'rule_version': VERSION,
            'grades_excluded': True}


__all__ = ['ACCOUNT_DOMAIN', 'EXAM_EVENT_STATUS', 'EXAM_RULE', 'SERVICE_DOMAIN',
           'TIMETABLE_RULE', 'VERSION', 'calendar_events', 'exam_event',
           'timetable_events']
