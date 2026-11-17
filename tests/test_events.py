"""5.4 unified agenda and incremental event contracts.

All fixtures are synthetic.  No campus service, model endpoint, message channel
or background process is contacted.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from thu_assistant import calendar_export, events, snapshot_diff
from thu_assistant import todos as t
from thu_assistant.bridge import CampusError

NOW = datetime.fromisoformat('2026-11-17T09:00:00+08:00')
CHECK = NOW.isoformat()
TZ = timezone.utc


def todo_id(value):
    return 'todo_' + hashlib.sha256(str(value).encode()).hexdigest()[:32]


def todo_row(value='1', *, source='learn.assignments', title='作业', due_at='2026-11-20',
             start_at=None, end_at=None, status='TODO', precision='date', **extra):
    row = {
        'event_id': todo_id(source + ':' + str(value)), 'source': source,
        'source_id': json.dumps(['course', str(value)]), 'title': title,
        'due_at': due_at, 'start_at': start_at, 'end_at': end_at,
        'date_precision': precision, 'status': status, 'source_status': {},
        'checked_at': CHECK, 'freshness': 'LIVE_AT_CHECKED_AT', 'warnings': [],
        'source_url': 'https://learn.tsinghua.edu.cn/',
    }
    row.update(extra)
    return row


def source_status(source='learn.assignments', complete=True, status='SUCCESS', pages=1):
    return {'source': source, 'status': status, 'complete': complete,
            'pages_read': pages, 'checked_at': CHECK}


def calendar_event(uid='CAMPUS:UNKNOWN:EXAM:exam-1:0', *, kind='EXAM', title='考试',
                   time=None, status='CONFIRMED', warnings=None, record='exam-1'):
    return {
        'event_uid': uid, 'account_domain': 'CAMPUS',
        'source': {'service_domain': 'UNKNOWN', 'kind': kind, 'record_id': record,
                   'stable_resource_uid': record},
        'title': title,
        'time': time or {'kind': 'DATE', 'date': '2026-11-20', 'timezone': 'Asia/Shanghai'},
        'status': status, 'sequence': 0, 'last_modified': CHECK,
        'source_evidence_id': 'academics.exams',
        'location': None, 'description': None, 'ics_uid': None,
        'export': {'state': 'NOT_EXPORTED', 'exported_at': None},
        'warnings': list(warnings or ()),
        'derivation': {'kind': 'DERIVED', 'rule_version': 'exam-event-v1',
                       'from': ['exam.state'], 'not_a_source_fact': True, 'occurrence': '0'},
    }


def interval(start='2026-11-18T10:00:00+08:00', end='2026-11-18T11:00:00+08:00'):
    return {'kind': 'DATE_TIME', 'start': start, 'end': end, 'timezone': 'Asia/Shanghai'}


def agenda(rows, academic=(), *, statuses=None, now=NOW):
    return events.build_agenda(rows, academic, statuses=statuses or (),
                               now=now, window={'start_date': '2026-11-17',
                                                'end_date': '2026-11-23'})


def test_stable_uid_is_independent_of_title_and_keeps_date_only_precision():
    first = events.from_todo(todo_row(title='旧标题', due_at='2026-11-20'))
    second = events.from_todo(todo_row(title='新标题', due_at='2026-11-20'))
    assert first['event_uid'] == second['event_uid']
    assert first['time'] == {'kind': 'DATE', 'date': '2026-11-20',
                             'timezone': 'Asia/Shanghai'}
    assert 'T00:00' not in json.dumps(first['time'])
    assert first['calendar_valid'] is True and first['time_trusted'] is False
    assert first['time_semantics'] == 'DEADLINE'
    assert events.to_calendar_event(first)['event_uid'] == first['event_uid']


def test_undated_item_is_explicit_and_never_receives_a_fake_date():
    item = events.from_todo(todo_row(due_at=None, status='TODO'))
    assert item['calendar_valid'] is False and item['event_uid'] is None
    assert item['time'] is None and item['time_precision'] == 'unknown'
    built = agenda([todo_row(due_at=None)])
    assert built['views']['undated']['count'] == 1
    assert built['calendar_events'] == [] and len(built['undated']) == 1
    with pytest.raises(CampusError, match='EXPORT_EVENT_INVALID'):
        calendar_export.select_events([item], {'start_date': '2026-11-17',
                                               'end_date': '2026-11-23'})


def test_same_title_from_two_sources_keeps_independent_identity():
    built = agenda([todo_row(title='同名事项')])
    academic = calendar_event(title='同名事项', uid='CAMPUS:UNKNOWN:EXAM:same-title:0')
    built = agenda([todo_row(title='同名事项')], [('academics.exams', academic)])
    uids = {item['event_uid'] for item in built['calendar_events']}
    assert len(uids) == 2
    assert {item['source_id'] for item in built['calendar_events']} == {
        'learn.assignments', 'academics.exams'}


def test_reviews_use_one_query_instant_and_follow_the_window():
    rows = [
        todo_row('today', due_at='2026-11-17'),
        todo_row('next', due_at='2026-11-18'),
        todo_row('late', due_at='2026-11-16'),
        todo_row('none', due_at=None),
        todo_row('done', due_at='2026-11-18', status='SUBMITTED'),
        todo_row('outside', due_at='2026-12-30'),
    ]
    built = agenda(rows, now=NOW)
    assert built['views']['upcoming']['count'] == 2
    assert built['views']['overdue']['count'] == 1
    assert built['views']['undated']['count'] == 1
    assert built['views']['completed']['count'] == 1
    assert built['views']['by_source']['learn.assignments']['count'] == 6
    assert built['as_of'] == NOW.isoformat(timespec='seconds')


def test_conflicts_only_use_trusted_intervals():
    seat = todo_row('seat', source='library.seat', title='座位', due_at=None,
                    start_at='2026-11-18T10:00:00+08:00', end_at='2026-11-18T11:00:00+08:00',
                    status='SCHEDULED', precision='minute')
    room = todo_row('room', source='library.room', title='研讨间', due_at=None,
                    start_at='2026-11-18T10:30:00+08:00', end_at='2026-11-18T11:30:00+08:00',
                    status='SCHEDULED', precision='minute')
    all_day = todo_row('exam', source='academics.exams', due_at=None, status='CONFIRMED',
                       precision='date')
    course = calendar_event(kind='COURSE', uid='CAMPUS:UNKNOWN:COURSE:c1:2026-11-18',
                            time=interval(start='2026-11-18T14:00:00+08:00',
                                          end='2026-11-18T15:00:00+08:00'),
                            warnings=['TIME_NOT_FROM_PERIOD_TABLE'])
    built = agenda([seat, room, all_day], [
        ('academics.timetable', course),
        ('academics.timetable', calendar_event(kind='COURSE',
                                               uid='CAMPUS:UNKNOWN:COURSE:c2:2026-11-18',
                                               time=interval(start='2026-11-18T14:00:00+08:00',
                                                             end='2026-11-18T15:00:00+08:00'))),
    ])
    assert len(built['conflicts']) == 1
    pair = built['conflicts'][0]
    assert pair['basis'] == 'TRUSTED_INTERVAL_OVERLAP'
    assert set(pair['sources']) == {'library.seat', 'library.room'}
    assert any(item['event_uid'].endswith('c2:2026-11-18') for item in built['calendar_events'])


def test_cancelled_and_unknown_states_never_participate_in_conflicts():
    cancelled = todo_row('seat', source='library.seat', due_at=None,
                         start_at='2026-11-18T10:00:00+08:00',
                         end_at='2026-11-18T11:00:00+08:00', status='CANCELLED',
                         precision='minute')
    other = todo_row('other', source='library.seat', due_at=None,
                     start_at='2026-11-18T10:15:00+08:00',
                     end_at='2026-11-18T10:45:00+08:00', status='SCHEDULED',
                     precision='minute')
    assert agenda([cancelled, other])['conflicts'] == []


def test_invalid_end_degrades_to_date_instead_of_inventing_an_interval():
    row = todo_row('bad', source='library.seat', due_at=None,
                   start_at='2026-11-18T11:00:00+08:00',
                   end_at='2026-11-18T10:00:00+08:00', status='SCHEDULED',
                   precision='minute')
    item = events.from_todo(row)
    assert item['time']['kind'] == 'DATE'
    assert 'TIME_RANGE_INVALID_DATE_ONLY' in item['warnings']
    assert item['time_trusted'] is False


def test_academic_capability_is_opt_in_and_blocked_by_default():
    class Bridge:
        def capabilities(self):
            return {'exams': {'implementation_state': 'BLOCKED',
                              'runtime_availability': 'UNAVAILABLE',
                              'start_conclusion': 'BLOCKED_SOURCE_EVIDENCE',
                              'evidence_level': 'NONE'}}

        def read(self, capability):
            raise AssertionError('a blocked capability must not be read')

    engine = _engine(academic_bridge=Bridge())
    plain = engine.get()
    assert len(plain['sources_status']) == 5
    assert 'academics.exams' not in {row['source'] for row in plain['sources_status']}
    selected = engine.get(services=['academics.exams'])
    status = selected['sources_status'][-1]
    assert selected['status'] == 'PARTIAL' and selected['complete'] is False
    assert status['source'] == 'academics.exams' and status['status'] == 'UNKNOWN'
    assert status['errors'] == [{'code': 'ADAPTER_REQUIRED'}]
    assert 'SOURCE_CONTRACT_BLOCKED' in status['warnings']
    assert selected['items'] == []


def test_grades_are_not_a_selectable_agenda_source():
    engine = _engine()
    with pytest.raises(CampusError, match='INVALID_TODO_SERVICES'):
        engine.get(services=['academics.grades'])
    with pytest.raises(CampusError, match='INVALID_TODO_SERVICES'):
        engine.get(services=['grades'])
    assert 'grades' not in t.ACADEMIC_SOURCES
    assert 'grades' not in engine.describe_agenda()['academic_sources']


def test_available_academic_capability_enters_only_when_selected():
    exam = calendar_event(title='合成考试', time=interval())
    course = calendar_event(kind='COURSE', uid='CAMPUS:UNKNOWN:COURSE:c1:2026-11-18',
                            title='合成课程', time=interval())

    class Bridge:
        def capabilities(self):
            return {'exams': {'implementation_state': 'READY',
                              'runtime_availability': 'AVAILABLE',
                              'start_conclusion': 'CONTRACT_READY',
                              'evidence_level': 'SYNTHETIC_TRANSPORT'},
                    'timetable': {'implementation_state': 'READY',
                                  'runtime_availability': 'AVAILABLE',
                                  'start_conclusion': 'CONTRACT_READY',
                                  'evidence_level': 'SYNTHETIC_TRANSPORT'}}

        def read(self, capability):
            return {'events': [exam] if capability == 'exams' else [course],
                    'excluded': [], 'complete': True, 'warnings': [],
                    'checked_at': CHECK, 'source_version': 'academics-event-v1'}

    engine = _engine(academic_bridge=Bridge())
    assert engine.get()['items'] == []
    result = engine.get(services=['academics'], semester='2026-FALL')
    assert [row['source'] for row in result['sources_status']] == [
        'academics.exams', 'academics.timetable']
    assert result['status'] == 'OK' and result['source_complete'] is True
    assert len(result['academic_events']) == 2
    agenda = engine.agenda(services=['academics'], semester='2026-FALL')
    assert len(agenda['calendar_events']) == 2
    assert agenda['views']['by_source']['academics.exams']['count'] == 1


def test_agenda_does_not_poll_or_send_messages():
    engine = _engine()
    engine.get = lambda **kwargs: {
        'status': 'EMPTY', 'checked_at': CHECK, 'timezone': 'Asia/Shanghai',
        'window': {'start_date': '2026-11-17', 'end_date': '2026-11-23', 'inclusive': True},
        'sources_status': [], 'source_complete': True, 'scope_notes': [],
        'selection': {'sources': [], 'academic': [], 'semester': None},
        'items': [],
    }
    result = engine.agenda()
    assert result['background_polling'] is False
    assert result['message_delivery'] is False
    assert result['delivery_state'] == 'NOT_DELIVERED'
    assert engine.describe_agenda()['background_polling'] is False


def test_snapshot_views_and_change_ids_are_repeatable():
    scope = _scope()
    previous = _snapshot([events.from_todo(todo_row('1'))])
    current = _snapshot([events.from_todo(todo_row('2'))])
    first = snapshot_diff.compare(previous, deepcopy(current), account='A', scope=scope, now=NOW)
    second = snapshot_diff.compare(previous, deepcopy(current), account='A', scope=scope, now=NOW)
    assert [change['change_id'] for change in first['changes']] == [
        change['change_id'] for change in second['changes']]
    assert first['changes'][0]['type'] == 'ADDED'
    third = snapshot_diff.compare(previous, deepcopy(previous), account='A', scope=scope, now=NOW)
    assert third['changes'] == []


def test_added_modified_cancelled_and_deleted():
    scope = _scope()
    baseline = _snapshot([events.from_todo(todo_row('1', title='旧'))])
    modified = _snapshot([events.from_todo(todo_row('1', title='新'))])
    result = snapshot_diff.compare(baseline, modified, account='A', scope=scope, now=NOW)
    assert [change['type'] for change in result['changes']] == ['MODIFIED']
    assert result['changes'][0]['sequence'] == 1
    cancelled = _snapshot([events.from_todo(todo_row('1', title='新', status='CANCELLED'))])
    result = snapshot_diff.compare(modified, cancelled, account='A', scope=scope, now=NOW)
    assert [change['type'] for change in result['changes']] == ['CANCELLED']
    empty = _snapshot([])
    result = snapshot_diff.compare(cancelled, empty, account='A', scope=scope, now=NOW)
    assert [change['type'] for change in result['changes']] == ['DELETED']


def test_failed_source_never_deletes_every_event():
    scope = _scope()
    baseline = _snapshot([events.from_todo(todo_row('1')),
                          events.from_todo(todo_row('2'))])
    failed = {'events': [], 'checked_at': CHECK,
              'sources': [source_status(complete=False, status='FAILED', pages=0)]}
    result = snapshot_diff.compare(baseline, failed, account='A', scope=scope, now=NOW)
    types = [change['type'] for change in result['changes']]
    assert 'SOURCE_UNAVAILABLE' in types and 'DELETED' not in types
    assert result['counts']['deleted'] == 0
    assert result['counts']['carried_forward'] == 2
    assert result['status'] == 'PARTIAL'
    assert all('HISTORICAL' == item['freshness'] for item in result['snapshot']['events'])


def test_partial_page_never_deletes_unseen_events():
    scope = _scope()
    baseline = _snapshot([events.from_todo(todo_row('1')),
                          events.from_todo(todo_row('2'))])
    partial = _snapshot([events.from_todo(todo_row('1'))], complete=False,
                        status='PARTIAL')
    result = snapshot_diff.compare(baseline, partial, account='A', scope=scope, now=NOW)
    types = [change['type'] for change in result['changes']]
    assert 'DELETED' not in types and 'SOURCE_INCOMPLETE' in types
    assert result['comparison']['learn.assignments']['deletions'] == 'SUPPRESSED_NOT_COMPARABLE'
    assert len(result['snapshot']['events']) == 2


def test_incomplete_baseline_cannot_make_a_deletion():
    scope = _scope()
    baseline = _snapshot([events.from_todo(todo_row('1'))], complete=False,
                         status='PARTIAL')
    current = _snapshot([])
    result = snapshot_diff.compare(baseline, current, account='A', scope=scope, now=NOW)
    assert 'DELETED' not in [change['type'] for change in result['changes']]
    assert len(result['snapshot']['events']) == 1


def test_scope_version_and_account_changes_never_compare():
    scope = _scope()
    baseline = _snapshot([events.from_todo(todo_row('1'))])
    changed_scope = {**scope, 'sources': ['library.loans']}
    result = snapshot_diff.compare(baseline, _snapshot([]), account='A',
                                   scope=changed_scope, now=NOW)
    assert result['baseline_state'] == 'REBUILT' and result['changes'] == []
    with pytest.raises(CampusError, match='SNAPSHOT_ACCOUNT_MISMATCH'):
        snapshot_diff.compare(baseline, _snapshot([]), account='B', scope=scope, now=NOW)
    result = snapshot_diff.compare(baseline, _snapshot([]), account='A', scope=scope,
                                   now=NOW, canonical_version='other-version')
    assert result['baseline_state'] == 'REBUILT' and result['changes'] == []


def test_cursor_is_opaque_versioned_scoped_and_expiring():
    times = [NOW]
    store = snapshot_diff.IncrementalStore(
        store=snapshot_diff.SnapshotStore(backend=snapshot_diff.MemoryBackend()),
        now=lambda: times[0])
    scope = _scope()
    current = _snapshot([events.from_todo(todo_row('1'))])
    first = store.query(current, account='A', scope=scope)
    assert first['next_cursor'].startswith('evc1.')
    assert '2026' not in first['next_cursor']
    assert first['cursor_scope'] == 'PROCESS_LOCAL'
    second = store.query(current, account='A', scope=scope, cursor=first['next_cursor'])
    assert second['baseline_source'] == 'CURSOR'
    with pytest.raises(CampusError, match='EVENT_CURSOR_SCOPE_MISMATCH'):
        store.query(current, account='A', scope={**scope, 'start_date': '2026-11-18'},
                    cursor=first['next_cursor'])
    with pytest.raises(CampusError, match='EVENT_CURSOR_INVALID'):
        store.query(current, account='A', scope=scope, cursor='2026-11-17')
    times[0] = NOW.replace(hour=10)
    with pytest.raises(CampusError, match='EVENT_CURSOR_EXPIRED'):
        store.query(current, account='A', scope=scope, cursor=first['next_cursor'])


def test_cursor_does_not_claim_delivery_and_has_no_delivered_at():
    engine = snapshot_diff.IncrementalStore(
        store=snapshot_diff.SnapshotStore(backend=snapshot_diff.MemoryBackend()),
        now=lambda: NOW)
    result = engine.query(_snapshot([events.from_todo(todo_row('1'))]),
                          account='A', scope=_scope())
    assert result['delivery_state'] == 'NOT_DELIVERED'
    assert result['delivery_evidence'] is False and result['pushed'] is False
    assert 'delivered_at' not in result
    assert all('delivered_at' not in change for change in result['changes'])


def test_snapshot_store_is_encrypted_and_rejects_corruption(tmp_path):
    codec = lambda value: bytes(byte ^ 0x5A for byte in value)
    store = snapshot_diff.SnapshotStore(tmp_path, protect=codec, unprotect=codec)
    record = {'version': snapshot_diff.VERSION, 'account': 'A',
              'scope_digest': 'x' * 64, 'checked_at': CHECK,
              'canonical_version': snapshot_diff.VERSION, 'scope': _scope(),
              'events': [{'title': 'private-title'}], 'snapshot_id': 'snap_x'}
    store.save(record)
    path = store._path('A', 'x' * 64)
    assert b'private-title' not in path.read_bytes()
    assert store.load('A', 'x' * 64)['events'][0]['title'] == 'private-title'
    with pytest.raises(CampusError, match='SNAPSHOT_CORRUPT'):
        snapshot_diff.SnapshotStore._validate({**record, 'account': 'B'}, 'A', 'x' * 64)
    path.write_bytes(b'bad')
    with pytest.raises(CampusError, match='SNAPSHOT_CORRUPT'):
        store.load('A', 'x' * 64)
    failing = snapshot_diff.SnapshotStore(tmp_path, protect=lambda value: None)
    with pytest.raises(CampusError, match='SNAPSHOT_ENCRYPT_FAILED'):
        failing.save(record)
    assert not list(Path(tmp_path).glob('*.tmp'))


def test_every_5_4_error_code_is_registered():
    root = Path(__file__).resolve().parents[1]
    contract = json.loads((root / 'contracts/v0.2/error-classification.json').read_text(
        encoding='utf-8'))['codes']
    codes = {
        'EVENT_INVALID_RECORD': 'INPUT', 'EVENT_INVALID_TIME': 'INPUT',
        'EVENT_UNKNOWN_SOURCE': 'INPUT', 'EVENT_UID_CONFLICT': 'CONFLICT',
        'INVALID_ACADEMIC_SEMESTER': 'INPUT', 'ACADEMICS_SOURCE_BLOCKED': 'NOT_IMPLEMENTED',
        'SNAPSHOT_SCOPE_REQUIRED': 'INPUT', 'SNAPSHOT_SCOPE_INVALID': 'INPUT',
        'SNAPSHOT_ACCOUNT_MISMATCH': 'CONFLICT', 'SNAPSHOT_CORRUPT': 'INTERNAL',
        'SNAPSHOT_WRITE_FAILED': 'INTERNAL', 'SNAPSHOT_ENCRYPT_FAILED': 'INTERNAL',
        'SNAPSHOT_TOO_LARGE': 'COMPLETENESS', 'EVENT_CURSOR_INVALID': 'INPUT',
        'EVENT_CURSOR_EXPIRED': 'COMPLETENESS', 'EVENT_CURSOR_SCOPE_MISMATCH': 'CONFLICT',
        'EXPORT_SCOPE_REQUIRED': 'INPUT', 'EXPORT_SCOPE_INVALID': 'INPUT',
        'EXPORT_EVENTS_REQUIRED': 'INPUT', 'EXPORT_SOURCE_INCOMPLETE': 'COMPLETENESS',
        'EXPORT_EVENT_INVALID': 'INPUT', 'EXPORT_DUPLICATE_UID': 'CONFLICT',
        'EXPORT_DESTINATION_REQUIRED': 'INPUT', 'EXPORT_DESTINATION_INVALID': 'INPUT',
        'EXPORT_DESTINATION_EXISTS': 'CONFLICT', 'EXPORT_WRITE_FAILED': 'INTERNAL',
        'EXPORT_TOO_LARGE': 'COMPLETENESS', 'AGENDA_RESULT_TRUNCATED': 'COMPLETENESS',
        'EXPORT_FORMAT_UNSUPPORTED': 'INPUT',
    }
    from thu_assistant import source_state
    for code, category in codes.items():
        assert contract[code] == category
        assert source_state.ERROR_CODES[code] == category


def test_modules_have_no_network_polling_or_messaging_path():
    root = Path(__file__).resolve().parents[1] / 'src' / 'thu_assistant'
    forbidden = ('threading', 'asyncio', 'subprocess', 'requests', 'urlopen',
                 'model_chat', 'wechat', 'smtplib')
    for name in ('events.py', 'snapshot_diff.py', 'calendar_export.py', 'todos.py'):
        text = (root / name).read_text(encoding='utf-8')
        assert not [word for word in forbidden if word in text], name


def test_agenda_and_changes_do_not_start_threads():
    import threading
    before = threading.active_count()
    engine = _engine()
    engine.agenda()
    engine.changes(start_date='2026-11-17', end_date='2026-11-23')
    assert threading.active_count() == before


def test_stable_uid_survives_a_status_change():
    scheduled = events.from_todo(todo_row('1', status='SCHEDULED'))
    cancelled = events.from_todo(todo_row('1', status='CANCELLED'))
    assert scheduled['event_uid'] == cancelled['event_uid']
    assert scheduled['status'] == 'CONFIRMED' and cancelled['status'] == 'CANCELLED'


def test_expired_baseline_rebuilds_instead_of_inventing_changes():
    scope = _scope()
    baseline = _snapshot([events.from_todo(todo_row('1'))])
    baseline['checked_at'] = '2026-11-01T09:00:00+08:00'
    result = snapshot_diff.compare(baseline, _snapshot([events.from_todo(todo_row('1'))]),
                                   account='A', scope=scope, now=NOW)
    assert result['baseline_state'] == 'REBUILT' and result['changes'] == []
    assert 'BASELINE_EXPIRED' in result['warnings']


def test_agenda_end_to_end_keeps_legacy_items_and_builds_conflicts():
    seat_rows = [
        {'reservation_id': '1', 'location': '座位A', 'status_label': '已预约',
         'start_time': '2026-11-18 10:00', 'end_time': '2026-11-18 11:00'},
        {'reservation_id': '2', 'location': '座位B', 'status_label': '已预约',
         'start_time': '2026-11-18 10:30', 'end_time': '2026-11-18 11:30'},
    ]
    engine = _engine({'library.seat': {'items': seat_rows, 'status': 'OK',
                                       'source_complete': True, 'complete': True,
                                       'checked_at': CHECK, 'next_cursor': None}})
    built = engine.agenda(services=['library.seat'])
    assert built['status'] == 'OK' and len(built['calendar_events']) == 2
    assert len(built['conflicts']) == 1
    assert built['views']['by_source']['library.seat']['count'] == 2
    assert built['message_delivery'] is False


def test_export_calendar_assigns_snapshot_sequences(tmp_path):
    rows = [{'loan_id': '1', 'title': '借阅', 'due_date': '2026-11-20', 'due_time': None}]
    responses = {'library.loans': {'items': rows, 'status': 'OK',
                                   'source_complete': True, 'complete': True,
                                   'checked_at': CHECK, 'next_cursor': None}}
    engine = _engine(responses)
    target = tmp_path / 'loans.ics'
    first = engine.export_agenda(target, start_date='2026-11-17',
                                 end_date='2026-11-23',
                                 services=['library.loans'])
    assert first['event_count'] == 1
    assert first['source_complete'] is True
    responses['library.loans']['items'] = [{**rows[0], 'title': '借阅（更新）'}]
    second = engine.export_agenda(target, start_date='2026-11-17',
                                  end_date='2026-11-23',
                                  services=['library.loans'], overwrite=True)
    assert second['source_complete'] is True
    text = target.read_text(encoding='utf-8').replace('\r\n ', '')
    assert 'SEQUENCE:1' in text and '借阅（更新）' in text
    with pytest.raises(CampusError, match='EXPORT_SCOPE_REQUIRED'):
        engine.export_agenda(target, services=['library.loans'])


def test_export_calendar_is_pure_and_does_not_advance_changes(tmp_path):
    calls = []
    responses = {'library.loans': {'items': [], 'status': 'OK',
                                   'source_complete': True, 'complete': True,
                                   'checked_at': CHECK, 'next_cursor': None}}

    def query(service, operation, parameters, **kwargs):
        calls.append((service, operation))
        return responses['library.loans']

    engine = _engine(query)
    before = engine.changes(start_date='2026-11-17', end_date='2026-11-23',
                            services=['library.loans'])
    calls.clear()
    event = events.from_todo(todo_row('1', source='library.loans',
                                      due_at='2026-11-20', title='事项'))
    written = engine.export_calendar(tmp_path / 'pure.ics', [event],
                                     scope={'start_date': '2026-11-17',
                                            'end_date': '2026-11-23'})
    assert written['event_count'] == 1
    assert calls == []
    after = engine.changes(start_date='2026-11-17', end_date='2026-11-23',
                           services=['library.loans'])
    assert after['baseline_state'] == 'COMPARABLE'
    assert after['changes'] == before['changes'] == []


def test_academic_events_outside_the_window_are_not_in_agenda_or_changes():
    class Bridge:
        def capabilities(self):
            return {'exams': {'implementation_state': 'READY',
                              'runtime_availability': 'AVAILABLE',
                              'start_conclusion': 'CONTRACT_READY'}}

        def read(self, capability, **kwargs):
            return {'events': [calendar_event(date='2026-12-30')],
                    'excluded': [], 'complete': True, 'warnings': [],
                    'checked_at': CHECK, 'pages_read': 1}

    engine = _engine(academic_bridge=Bridge())
    built = engine.agenda(start_date='2026-11-17', end_date='2026-11-23',
                          services=['academics.exams'])
    assert built['calendar_events'] == []
    assert built['views']['upcoming']['count'] == 0
    assert built['status'] != 'OK'
    engine.changes(start_date='2026-11-17', end_date='2026-11-23',
                   services=['academics.exams'])
    changed = engine.changes(start_date='2026-11-17', end_date='2026-11-23',
                             services=['academics.exams'])
    assert not [c for c in changed['changes']
                if c['type'] in ('ADDED', 'DELETED')]


def test_agenda_collects_every_snapshot_page_instead_of_truncating():
    rows = [{'course_id': 'c', 'base_id': str(i), 'title': f'a{i}',
             'deadline': '2026-11-20', 'submitted': False} for i in range(201)]
    engine = _engine({'learn.assignments': {'items': rows, 'status': 'OK',
                                            'source_complete': True, 'complete': True,
                                            'checked_at': CHECK, 'next_cursor': None}})
    built = engine.agenda(start_date='2026-11-17', end_date='2026-11-23',
                          services=['learn'])
    assert len(built['calendar_events']) == 201
    assert built['status'] == 'OK' and built['source_complete'] is True
    assert built['pagination']['next_cursor'] is None
    changes = engine.changes(start_date='2026-11-17', end_date='2026-11-23',
                             services=['learn'])
    assert changes['status'] in ('OK', 'EMPTY')


def test_cross_midnight_interval_on_the_last_window_day_is_upcoming():
    row = todo_row('seat', source='library.seat', due_at=None,
                   start_at='2026-11-23T23:00:00+08:00',
                   end_at='2026-11-24T01:00:00+08:00', status='SCHEDULED',
                   precision='minute')
    built = agenda([row], now=NOW)
    assert len(built['calendar_events']) == 1
    assert built['views']['upcoming']['count'] == 1


def test_invalid_calendar_events_are_excluded_without_native_exceptions():
    bad_events = [
        calendar_event(time={'kind': 'DATE', 'date': '2026-13-45',
                             'timezone': 'Asia/Shanghai'}),
        calendar_event(time=interval(start='2026-11-20T11:00:00+08:00',
                                     end='2026-11-20T10:00:00+08:00')),
        {**calendar_event(), 'title': ''},
        {**calendar_event(), 'last_modified': None},
        {**calendar_event(), 'account_domain': 'ATTACKER'},
    ]
    built = events.build_agenda([], [('academics.exams', value) for value in bad_events],
                                now=NOW, window={'start_date': '2026-11-17',
                                                 'end_date': '2026-11-23'})
    assert built['calendar_events'] == []
    assert len(built['excluded']) == len(bad_events)
    assert all(row['reason'] in ('EVENT_INVALID_RECORD', 'EVENT_INVALID_TIME')
               for row in built['excluded'])


def test_bad_sequence_and_non_dict_todo_rows_degrade_to_excluded():
    item = events.from_todo(todo_row('1', sequence='not-an-int'))
    assert item['sequence'] == 0 and 'SEQUENCE_INVALID' in item['warnings']
    built = events.build_agenda(['not-a-row'], now=NOW,
                                window={'start_date': '2026-11-17',
                                        'end_date': '2026-11-23'})
    assert built['items'] == []
    assert built['excluded'][0]['reason'] == 'EVENT_INVALID_RECORD'


def test_agenda_and_changes_validate_against_the_result_envelope():
    import jsonschema
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / 'contracts/v0.2/result-envelope.schema.json').read_text(
        encoding='utf-8'))
    engine = _engine({'learn.assignments': {'items': [todo_row('1')], 'status': 'OK',
                                            'source_complete': True, 'complete': True,
                                            'checked_at': CHECK, 'next_cursor': None}})
    built = engine.agenda(start_date='2026-11-17', end_date='2026-11-23',
                          services=['learn'])
    jsonschema.validate(built, schema)
    changes = engine.changes(start_date='2026-11-17', end_date='2026-11-23',
                             services=['learn'])
    jsonschema.validate(changes, schema)


def test_default_incremental_store_keeps_a_process_local_baseline_without_a_key(tmp_path):
    store = snapshot_diff.SnapshotStore(tmp_path, protect=lambda value: None)
    engine = snapshot_diff.IncrementalStore(store=store, now=lambda: NOW)
    current = _snapshot([events.from_todo(todo_row('1'))])
    first = engine.query(current, account='A', scope=_scope())
    second = engine.query(current, account='A', scope=_scope())
    assert first['baseline_state'] == 'CREATED'
    assert second['baseline_state'] == 'COMPARABLE'
    assert second['baseline_persisted'] is False


def test_cursor_is_explicitly_process_local():
    backend = snapshot_diff.MemoryBackend()
    scope = _scope()
    current = _snapshot([events.from_todo(todo_row('1'))])
    first = snapshot_diff.IncrementalStore(
        store=snapshot_diff.SnapshotStore(backend=backend), now=lambda: NOW)
    token = first.query(current, account='A', scope=scope)['next_cursor']
    second = snapshot_diff.IncrementalStore(
        store=snapshot_diff.SnapshotStore(backend=backend), now=lambda: NOW)
    with pytest.raises(CampusError, match='EVENT_CURSOR_EXPIRED'):
        second.query(current, account='A', scope=scope, cursor=token)


def test_academic_bridge_forwards_semester_and_expansions(monkeypatch):
    calls = {}

    class Reader:
        def read(self, capability, **kwargs):
            calls.update(capability=capability, kwargs=kwargs)
            return {'items': [], 'implementation_state': 'READY', 'complete': True,
                    'checked_at': CHECK, 'read_scope': {'pages_read': 2},
                    'warnings': []}

    monkeypatch.setattr('thu_assistant.academics.reader.AcademicsReader', Reader)
    result = events.AcademicBridge().read('timetable', semester='2026-FALL',
                                          expansions={'c1': {}})
    assert calls == {'capability': 'timetable',
                     'kwargs': {'semester': '2026-FALL'}}
    assert result['pages_read'] == 2


def test_get_todos_description_mentions_the_opt_in_academic_path():
    from thu_assistant.mcp_stdio import TOOL_BY_NAME
    description = TOOL_BY_NAME['get_todos']['description']
    assert 'ADAPTER_REQUIRED' in description
    assert '学业' in description


def test_export_agenda_rejects_an_incomplete_source_unless_allowed(tmp_path):
    responses = {'library.loans': {'items': [], 'status': 'FAILED',
                                   'source_complete': False, 'complete': False,
                                   'checked_at': CHECK, 'next_cursor': None,
                                   'source_errors': [{'code': 'AUTH_REQUIRED'}]}}
    engine = _engine(responses)
    with pytest.raises(CampusError, match='EXPORT_SOURCE_INCOMPLETE'):
        engine.export_agenda(tmp_path / 'failed.ics', start_date='2026-11-17',
                             end_date='2026-11-23', services=['library.loans'])
    written = engine.export_agenda(tmp_path / 'partial.ics', start_date='2026-11-17',
                                   end_date='2026-11-23', services=['library.loans'],
                                   allow_partial=True)
    assert written['source_complete'] is False
    assert 'X-OPENTHU-SOURCE-COMPLETE:false' in (tmp_path / 'partial.ics').read_text(
        encoding='utf-8')


def test_sequence_does_not_reset_after_expiry_scope_change_or_no_key(tmp_path):
    import re
    times = [NOW]
    responses = {'library.loans': {'items': [{'loan_id': '1', 'title': 'old',
                                               'due_date': '2026-11-20', 'due_time': None}],
                                    'status': 'OK', 'source_complete': True,
                                    'complete': True, 'checked_at': CHECK,
                                    'next_cursor': None}}
    engine = _engine(responses)
    engine.now = lambda: times[0]
    engine.incremental.now = lambda: times[0]
    engine.incremental.cursors.clock = lambda: times[0]
    target = tmp_path / 'seq.ics'
    engine.export_agenda(target, start_date='2026-11-17', end_date='2026-11-23',
                         services=['library.loans'])
    assert 'SEQUENCE:0' in target.read_text(encoding='utf-8').replace('\r\n ', '')
    responses['library.loans']['items'][0]['title'] = 'new'
    times[0] = NOW.replace(minute=10)
    engine.export_agenda(target, start_date='2026-11-17', end_date='2026-11-23',
                         services=['library.loans'], overwrite=True)
    first = int(re.search(r'SEQUENCE:(\d+)', target.read_text(encoding='utf-8')).group(1))
    assert first >= 1
    engine.export_agenda(target, start_date='2026-11-18', end_date='2026-11-24',
                         services=['library.loans'], overwrite=True)
    shifted = int(re.search(r'SEQUENCE:(\d+)', target.read_text(encoding='utf-8')).group(1))
    assert shifted >= first
    times[0] = NOW.replace(day=25)
    engine.export_agenda(target, start_date='2026-11-17', end_date='2026-11-23',
                         services=['library.loans'], overwrite=True)
    expired = int(re.search(r'SEQUENCE:(\d+)', target.read_text(encoding='utf-8')).group(1))
    assert expired >= shifted

    no_key = _engine(responses)
    no_key.incremental = snapshot_diff.IncrementalStore(
        store=snapshot_diff.SnapshotStore(tmp_path / 'nokey',
                                          protect=lambda value: None),
        now=lambda: NOW)
    path = tmp_path / 'nokey.ics'
    no_key.export_agenda(path, start_date='2026-11-17', end_date='2026-11-23',
                         services=['library.loans'])
    responses['library.loans']['items'][0]['title'] = 'newer'
    no_key.export_agenda(path, start_date='2026-11-17', end_date='2026-11-23',
                         services=['library.loans'], overwrite=True)
    assert int(re.search(r'SEQUENCE:(\d+)', path.read_text(encoding='utf-8')).group(1)) >= 1


def test_partial_academic_read_is_source_incomplete_not_unavailable():
    class Bridge:
        def capabilities(self):
            return {'exams': {'implementation_state': 'READY',
                              'runtime_availability': 'AVAILABLE',
                              'start_conclusion': 'CONTRACT_READY'}}

        def read(self, capability, **kwargs):
            return {'events': [calendar_event()], 'excluded': [],
                    'complete': False, 'warnings': [], 'checked_at': CHECK,
                    'pages_read': 1}

    engine = _engine(academic_bridge=Bridge())
    engine.changes(start_date='2026-11-17', end_date='2026-11-23',
                   services=['academics.exams'])
    changed = engine.changes(start_date='2026-11-17', end_date='2026-11-23',
                             services=['academics.exams'])
    types = [change['type'] for change in changed['changes']]
    assert 'SOURCE_INCOMPLETE' in types
    assert 'SOURCE_UNAVAILABLE' not in types


# --------------------------------------------------------------------------
# engine fixtures


class Cache:
    def __init__(self):
        self.data = {}

    def save(self, account, source, key, data):
        self.data[account, source, key] = deepcopy(data)

    def load(self, account, source, key, now):
        return deepcopy(self.data.get((account, source, key)))


def _engine(responses=None, academic_bridge=None, now=NOW):
    responses = responses or {}
    calls = []

    def query(service, operation, parameters, **kwargs):
        calls.append((service, operation, deepcopy(parameters), kwargs))
        source = ('learn.assignments' if service == 'learn' else
                  'library.loans' if operation == 'loans' else
                  'sports.orders' if service == 'sports' else
                  'library.' + parameters['system'])
        value = responses.get(source, {'items': [], 'status': 'OK',
                                       'source_complete': True, 'complete': True,
                                       'checked_at': CHECK, 'next_cursor': None})
        return deepcopy(value)

    owner = SimpleNamespace(queries=SimpleNamespace(query=query),
                            bridge=lambda *a, **k: {'account_id': 'A'})
    engine = t.TodoEngine(owner, cache=Cache(), now=lambda: now,
                          academic_bridge=academic_bridge,
                          incremental=snapshot_diff.IncrementalStore(
                              store=snapshot_diff.SnapshotStore(
                                  backend=snapshot_diff.MemoryBackend()),
                              now=lambda: now))
    return engine


def _scope():
    return {'start_date': '2026-11-17', 'end_date': '2026-11-23',
            'sources': ['learn.assignments'], 'include_completed': False,
            'include_overdue': True, 'semester': None,
            'allow_stale': False, 'max_source_pages': 3}


def _source_record(source='learn.assignments', complete=True, status='SUCCESS', pages=1):
    return {'status': status, 'complete': complete, 'pages_read': pages,
            'checked_at': CHECK, 'error_codes': [], 'warnings': []}


def _snapshot(items, *, complete=True, status='SUCCESS', source='learn.assignments',
              scope=None, account='A', version=snapshot_diff.VERSION):
    scope = scope or _scope()
    scope_hash = snapshot_diff.scope_digest({key: scope.get(key)
                                             for key in snapshot_diff.SCOPE_KEYS})
    return {'version': snapshot_diff.VERSION, 'canonical_version': version,
            'account': account, 'scope_digest': scope_hash, 'scope': scope,
            'checked_at': CHECK, 'snapshot_id': 'snap_test',
            'events': deepcopy(items),
            'sources': {source: _source_record(
                source, complete=complete, status=status,
                pages=1 if status != 'FAILED' else 0)}}
