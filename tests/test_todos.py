from copy import deepcopy
from datetime import datetime
import json
import threading
from types import SimpleNamespace
import pytest
from thu_assistant import todos as t
from thu_assistant.bridge import CampusError, request_control
from thu_assistant.mcp_stdio import CampusAssistant

NOW = datetime.fromisoformat('2026-11-06T00:01:00+08:00')
CHECK = NOW.isoformat()


def assignment(id='1', **kwargs):
    return dict(id=id, course_id='c1', course_name='同名课', title='作业', deadline='2026-11-07',
                submitted=False, graded=False, **kwargs)


def response(items=None, **kwargs):
    return dict(items=items or [], status='OK', source_complete=True, complete=True,
                checked_at=CHECK, next_cursor=None, **kwargs)


class Cache:
    def __init__(self):
        self.data = {}
    def save(self, account, source, key, data):
        self.data[account, source, key] = deepcopy(data)
    def load(self, account, source, key, now):
        return deepcopy(self.data.get((account, source, key)))


def engine(responses=None, now=None):
    responses = responses or {}
    calls = []
    identity = ['A']
    def query(service, operation, parameters, **kwargs):
        calls.append((service, operation, deepcopy(parameters), kwargs))
        source = 'learn.assignments' if service == 'learn' else 'library.loans' if operation == 'loans' else 'sports.orders' if service == 'sports' else 'library.' + parameters['system']
        value = responses.get(source, response())
        if callable(value):
            return value(parameters, kwargs)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)
    owner = SimpleNamespace(queries=SimpleNamespace(query=query), bridge=lambda *a, **k: {'account_id': identity[0]})
    return t.TodoEngine(owner, cache=Cache(), now=now or (lambda: NOW)), calls, identity


def test_default_window_scope_and_real_empty():
    e, calls, _ = engine()
    r = e.get()
    assert r['status'] == 'EMPTY' and r['complete'] and r['items'] == []
    assert r['window'] == {'start_date': '2026-11-06', 'end_date': '2026-11-12', 'inclusive': True}
    assert len(calls) == len(r['sources_status']) == 5
    assert calls[-2][2]['status_mask'] == 65535
    assert r['sources_status'][0]['scope'] == 'CURRENT_SEMESTER'


@pytest.mark.parametrize('value,precision,expected', [
    ('2026-11-06', 'date', '2026-11-06'),
    ('2026-11-05T16:01:00Z', 'second', '2026-11-06T00:01:00+08:00'),
    ('2026-11-06 12:30', 'minute', '2026-11-06T12:30+08:00'),
    ('1704067200000', 'millisecond', '2024-01-01T08:00:00.000+08:00'),
    (None, 'unknown', None),
])
def test_date_precision(value, precision, expected):
    assert t.timestamp(value) == (expected, precision)


def test_midnight_date_only_overdue_and_submitted_waiting_grade():
    items = []
    for i, deadline, submitted, graded in [
        ('1', '2026-11-06', False, False), ('2', '2026-11-05', False, False),
        ('3', '2026-11-05T23:59:00+08:00', False, False),
        ('4', '2026-11-07', True, False), ('5', '2026-11-07', True, True),
        ('6', None, False, False), ('7', '2026-11-05', None, None),
    ]:
        row = assignment(i)
        row.update(deadline=deadline, submitted=submitted, graded=graded)
        items.append(row)
    e, _, _ = engine({'learn.assignments': response(items)})
    r = e.get(services=['learn'])
    assert len(r['items']) == 4
    assert r['groups'] == {'overdue': 2, 'today': 1, 'unscheduled': 1}
    today = next(x for x in r['items'] if x['due_at'] == '2026-11-06')
    assert today['overdue'] is False and today['due_at'] == '2026-11-06'
    assert len(e.get(services=['learn'], include_overdue=False)['items']) == 2
    completed = e.get(services=['learn'], include_completed=True)
    assert {'SUBMITTED', 'GRADED'} <= {x['status'] for x in completed['items']}


def test_cancelled_unpaid_pending_approval_and_unknown():
    seats = [dict(reservation_id=str(i), location='座位', start_time='2026-11-06 10:00',
                  end_time='2026-11-06 11:00', status_label=label)
             for i, label in enumerate(['已取消', '待支付', '待审核', '新状态'])]
    rooms = [dict(reservation_id=str(i), rooms=[], start_time='2026-11-07 10:00',
                  end_time='2026-11-07 11:00', platform_status=state)
             for i, state in enumerate([256, 4, 260, 512])]
    e, _, _ = engine({'library.seat': response(seats), 'library.room': response(rooms)})
    r = e.get(services=['library'])
    states = [x['status'] for x in r['items']]
    assert len(states) == 6 and 'CANCELLED' not in states and 'REJECTED' not in states
    assert states.count('UNKNOWN') == 2 and 'PENDING_PAYMENT' in states
    assert 'PENDING_APPROVAL' in states and all(x['overdue'] is None for x in r['items'])
    assert len(e.get(services=['library'], include_completed=True)['items']) == 8


def test_same_names_and_duplicate_ids():
    a = assignment()
    b = {**a, 'course_id': 'c2'}
    c = {**a, 'id': '2'}
    e, _, _ = engine({'learn.assignments': response([a, a, b, c])})
    r = e.get(services=['learn'])
    assert len(r['items']) == 3 and len({x['event_id'] for x in r['items']}) == 3
    assert len([x for x in r['items'] if x.get('possible_duplicate_of')]) == 2


def test_conflicting_duplicate_is_not_silently_completed():
    a = assignment()
    e, _, _ = engine({'learn.assignments': response([a, {**a, 'submitted': True}])})
    r = e.get(services=['learn'])
    assert r['status'] == 'PARTIAL' and len(r['items']) == 1
    assert r['items'][0]['status'] == 'UNKNOWN'
    assert 'CONFLICTING_DUPLICATE' in r['sources_status'][0]['warnings']


def test_failed_source_retained_distinct_from_empty_and_secrets_not_echoed():
    e, _, _ = engine({'learn.assignments': RuntimeError('password=never-print')})
    r = e.get()
    assert not r['complete'] and r['status'] == 'PARTIAL'
    assert r['sources_status'][0]['status'] == 'FAILED'
    assert r['sources_status'][1]['status'] == 'SUCCESS'
    assert 'never-print' not in json.dumps(r)


def test_partial_source_items_and_errors_survive():
    e, _, _ = engine({'learn.assignments': {**response([assignment()]), 'source_complete': False,
                                           'status': 'PARTIAL', 'source_errors': [{'code': 'MISSING_PAGE'}]}})
    r = e.get(services=['learn'])
    assert len(r['items']) == 1 and not r['complete']
    assert r['sources_status'][0]['errors'] == [{'code': 'MISSING_PAGE'}]


def test_page_limit_is_not_complete_and_next_page_visible():
    def pages(p, _):
        row = dict(reservation_id=str(p['page']), location='座位', start_time='2026-11-07 10:00',
                   end_time='2026-11-07 11:00', status_label='已预约')
        return response([row], next_page=p['page']+1, upstream_total=80)
    e, calls, _ = engine({'library.seat': pages})
    r = e.get(services=['library.seat'])
    assert len(calls) == len(r['items']) == 3 and not r['complete']
    s = r['sources_status'][0]
    assert s['next_upstream_page'] == 4 and 'SOURCE_PAGE_LIMIT' in s['warnings']


def test_more_than_200_todos_use_one_immutable_account_bound_snapshot():
    e, calls, identity = engine({'learn.assignments': response([assignment(str(i)) for i in range(401)])})
    r = e.get(services=['learn'])
    token = r['next_cursor']
    assert len(r['items']) == 200 and not r['complete'] and r['source_complete']
    second = e.get(services=['learn'], cursor=token)
    last = e.get(services=['learn'], cursor=second['next_cursor'])
    assert len(last['items']) == 1 and last['complete'] and len(calls) == 1
    with pytest.raises(CampusError, match='CURSOR_QUERY_MISMATCH'):
        e.get(services=['learn'], cursor=token, include_completed=True)
    identity[0] = 'B'
    with pytest.raises(CampusError, match='CURSOR_QUERY_MISMATCH'):
        e.get(services=['learn'], cursor=token)
    other, _, _ = engine()
    with pytest.raises(CampusError, match='CURSOR_EXPIRED'):
        other.get(services=['learn'], cursor=token)


def test_midnight_cursor_retains_original_window():
    time = [NOW]
    e, calls, _ = engine({'learn.assignments': response([assignment(str(i)) for i in range(201)])},
                         now=lambda: time[0])
    r = e.get(services=['learn'])
    time[0] = datetime.fromisoformat('2026-11-07T00:01:00+08:00')
    p = e.get(services=['learn'], cursor=r['next_cursor'])
    assert p['window'] == r['window'] and p['checked_at'] == r['checked_at'] and len(calls) == 1


def test_stale_cache_is_opt_in_and_never_complete():
    responses = {'learn.assignments': response([assignment()])}
    e, _, _ = engine(responses)
    live = e.get(services=['learn'])
    responses['learn.assignments'] = CampusError('AUTH_REQUIRED')
    assert e.get(services=['learn'])['items'] == []
    old = e.get(services=['learn'], allow_stale=True)
    assert len(old['items']) == 1 and not old['complete']
    assert old['items'][0]['freshness'] == 'HISTORICAL'
    assert old['sources_status'][0]['checked_at'] == live['sources_status'][0]['checked_at']
    assert old['sources_status'][0]['errors'] == [{'code': 'AUTH_REQUIRED'}]


@pytest.mark.parametrize('args', [
    {'services': []}, {'services': 'learn'}, {'services': ['visitors']},
    {'services': ['learn', 'learn']}, {'services': [{}]},
    {'start_date': '2026-02-30'}, {'start_date': '2026-12-01', 'end_date': '2026-11-06'},
    {'start_date': '2026-01-01', 'end_date': '2028-01-01'},
    {'include_completed': 1}, {'max_source_pages': True}, {'max_source_pages': 51},
])
def test_bad_arguments_rejected_before_network(args):
    e, calls, _ = engine()
    with pytest.raises(CampusError):
        e.get(**args)
    assert not calls


def test_loans_precision_unknown_date_and_bad_date_partial():
    rows = [dict(loan_id='1', title='书', due_date='2026-11-06', due_time=None),
            dict(loan_id='2', title='书2', due_date='2026-11-06', due_time='00:00'),
            dict(loan_id='3', title='书3', due_date=None),
            dict(loan_id='4', title='书4', due_date='bad date')]
    e, _, _ = engine({'library.loans': response(rows)})
    r = e.get(services=['library.loans'])
    assert len(r['items']) == 3 and not r['complete']
    assert {x['date_precision'] for x in r['items']} == {'date', 'minute', 'unknown'}
    assert 'INVALID_SOURCE_RECORD' in r['sources_status'][0]['warnings']


def test_overnight_reservation_is_included_but_not_overdue():
    row = dict(reservation_id='1', rooms=[], start_time='2026-11-05 23:00',
               end_time='2026-11-06 01:00', platform_status=4)
    e, _, _ = engine({'library.room': response([row])})
    r = e.get(services=['library.room'])
    assert len(r['items']) == 1 and r['items'][0]['overdue'] is None


def test_cancel_stops_remaining_sources():
    flag = threading.Event()
    def first(*_):
        flag.set()
        return response([assignment()])
    e, calls, _ = engine({'learn.assignments': first})
    with request_control(flag), pytest.raises(CampusError, match='CANCELLED'):
        e.get()
    assert len(calls) == 1 and not e.cache.data


def test_account_change_prevents_snapshot_and_cache():
    e, _, identity = engine()
    def changed(*a, **k):
        identity[0] = 'B'
        return response([assignment()])
    e.owner.queries.query = changed
    with pytest.raises(CampusError, match='STATE_ACCOUNT_MISMATCH'):
        e.get(services=['learn'])
    assert not e.cache.data and not e.snapshots.entries


def test_feature_flag_leaves_individual_queries_available(monkeypatch):
    a = CampusAssistant(learning_factory=lambda: None, bridge=lambda *a, **k: {'account_id': 'A'})
    monkeypatch.setenv('THU_TODOS', '0')
    with pytest.raises(CampusError, match='TODOS_DISABLED'):
        a.call('get_todos', {})
    assert any(x['operation'] == 'assignments' for x in a.call('describe_service', {'service_id': 'learn'})['operations'])


def test_cache_no_plaintext_fallback_and_account_query_binding(tmp_path):
    c = t.TodoCache(tmp_path, protect=lambda _: None)
    with pytest.raises(CampusError, match='ENCRYPT_FAILED'):
        c.save('A', 'learn.assignments', 'key', {'items': [{'title': '秘密'}]})
    assert not list(tmp_path.glob('*'))
    # Synthetic reversible cipher verifies binding; real DPAPI covered separately on Windows.
    c = t.TodoCache(tmp_path, protect=lambda b: bytes(x ^ 90 for x in b), unprotect=lambda b: bytes(x ^ 90 for x in b))
    data = {'checked_at': CHECK, 'items': [{'title': 'private-book-title'}]}
    c.save('A', 'library.loans', 'key', data)
    assert b'private-book-title' not in c.path('A', 'library.loans').read_bytes()
    assert c.load('A', 'library.loans', 'key', NOW) == data
    assert c.load('B', 'library.loans', 'key', NOW) is None
    assert c.load('A', 'library.loans', 'other', NOW) is None
    assert c.load('A', 'library.loans', 'key', NOW.replace(day=20)) is None
    c.path('A', 'library.loans').write_bytes(b'bad')
    with pytest.raises(CampusError, match='CORRUPT'):
        c.load('A', 'library.loans', 'key', NOW)


def test_dpapi_roundtrip(tmp_path):
    import os
    if os.name != 'nt':
        pytest.skip('Windows DPAPI only')
    c = t.TodoCache(tmp_path)
    data = {'checked_at': CHECK, 'items': [{'title': 'synthetic-private-title'}]}
    c.save('A', 'library.loans', 'key', data)
    assert b'synthetic-private-title' not in c.path('A', 'library.loans').read_bytes()
    assert c.load('A', 'library.loans', 'key', NOW) == data


def test_overdue_false_excludes_elapsed_time_today():
    row = {**assignment(), 'deadline': '2026-11-06T00:00:00+08:00'}
    e, _, _ = engine({'learn.assignments': response([row])})
    assert len(e.get(services=['learn'])['items']) == 1
    assert e.get(services=['learn'], include_overdue=False)['items'] == []


def test_last_upstream_page_at_limit_can_be_complete():
    def page(p, _):
        return response([], next_page=2 if p['page'] == 1 else None, upstream_total=0)
    e, calls, _ = engine({'library.seat': page})
    r = e.get(services=['library.seat'], max_source_pages=2)
    assert len(calls) == 2 and r['complete']


def test_changed_upstream_total_is_partial():
    def page(p, _):
        return response([], next_page=2 if p['page'] == 1 else None, upstream_total=p['page'])
    e, _, _ = engine({'library.seat': page})
    r = e.get(services=['library.seat'])
    assert not r['complete'] and 'SOURCE_CHANGED_DURING_PAGING' in r['sources_status'][0]['warnings']


def test_source_snapshot_cursor_follows_same_parameters():
    def page(p, kw):
        if not kw['cursor']:
            return {**response([assignment('1')]), 'next_cursor': 'cursor1'}
        assert kw['cursor'] == 'cursor1' and p == {}
        return response([assignment('2')])
    e, calls, _ = engine({'learn.assignments': page})
    r = e.get(services=['learn'])
    assert len(calls) == 2 and len(r['items']) == 2 and r['complete']


def test_failure_after_read_preserves_partial_live_not_old_cache():
    def page(p, _):
        if p['page'] == 2:
            raise CampusError('FORBIDDEN')
        return response([dict(reservation_id='1', status_label='已预约', location='座位',
                              start_time='2026-11-07 10:00', end_time='2026-11-07 11:00')], next_page=2)
    e, _, _ = engine({'library.seat': page})
    r = e.get(services=['library.seat'], allow_stale=True)
    assert len(r['items']) == 1 and r['items'][0]['freshness'] == 'LIVE_AT_CHECKED_AT'
    assert r['sources_status'][0]['errors'] == [{'code': 'FORBIDDEN'}]
    assert not r['complete']


def test_stale_keeps_original_time_and_failed_refresh_time_separate():
    time = [NOW]
    responses = {'learn.assignments': response([assignment()])}
    e, _, _ = engine(responses, now=lambda: time[0])
    e.get(services=['learn'])
    time[0] = NOW.replace(hour=2)
    responses['learn.assignments'] = CampusError('AUTH_REQUIRED')
    r = e.get(services=['learn'], allow_stale=True)
    assert r['sources_status'][0]['checked_at'] == CHECK
    assert r['sources_status'][0]['refresh_attempted_at'] == time[0].isoformat()
    assert r['items'][0]['checked_at'] == CHECK


def test_unknown_source_state_never_empty_success():
    e, _, _ = engine({'library.room': {**response(), 'status': 'NEEDS_USER_ACTION'}})
    r = e.get(services=['library.room'])
    assert not r['complete'] and r['status'] == 'PARTIAL' and r['items'] == []


def test_repeated_record_across_pages_marks_source_incomplete():
    def page(p, kw):
        return {**response([assignment()]), 'next_cursor': None if kw['cursor'] else 'second'}
    e, _, _ = engine({'learn.assignments': page})
    r = e.get(services=['learn'])
    assert len(r['items']) == 1 and not r['complete']
    assert 'DUPLICATE_ACROSS_SOURCE_PAGES' in r['sources_status'][0]['warnings']


def test_official_used_seat_is_attended_not_an_upcoming_todo():
    row = dict(reservation_id='1', location='座位', status_label='已使用',
               start_time='2026-11-06 08:00', end_time='2026-11-06 12:00')
    e, _, _ = engine({'library.seat': response([row])})
    assert e.get(services=['library.seat'])['items'] == []
    r = e.get(services=['library.seat'], include_completed=True)
    assert r['items'][0]['status'] == 'ATTENDED'
    assert r['items'][0]['group'] == 'completed' and r['items'][0]['overdue'] is None

