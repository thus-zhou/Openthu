"""Module 3.2 acceptance tests: query completeness, identity, time, cache.

All fixtures are synthetic.  No campus, model or browser request is made.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import jsonschema
import pytest

from thu_assistant import cache_policy, catalog, domain_models, source_state
from thu_assistant.bridge import CampusError
from thu_assistant.queries import QueryEngine, Snapshots, binding, describe, metadata, page_metadata, read_scope_for

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / 'contracts' / 'v0.2'
TZ = timezone(timedelta(hours=8))


def contract(name):
    return json.loads((CONTRACTS / name).read_text(encoding='utf-8'))


def validate(name, value):
    jsonschema.Draft202012Validator(contract(name)).validate(value)


def library_owner(account='one'):
    return SimpleNamespace(bridge=lambda *a, **k: {'account_id': account}, browsers=None)


class LearningOwner:
    def __init__(self, result, account='one'):
        self.result = result
        self.account = account

    def bridge(self, *args, **kwargs):
        return {'account_id': self.account}

    def learning_call(self, name, args):
        return deepcopy(self.result)


def test_embedded_error_classification_matches_the_frozen_contract():
    frozen = contract('error-classification.json')
    assert source_state.ERROR_CODES == frozen['codes']
    assert set(source_state.CATEGORIES) == set(frozen['categories'])
    for name, rule in frozen['categories'].items():
        local = source_state.CATEGORIES[name]
        assert local['retryable'] is rule['retryable']
        assert local['user_action'] == rule['user_action']
    assert all(category in source_state.CATEGORIES for category in source_state.ERROR_CODES.values())


def test_public_error_never_copies_exception_or_private_context():
    err = source_state.public_error('SOMETHING_FAILED')
    assert err['code'] == 'UNKNOWN' and err['category'] == 'INTERNAL'
    assert err['automatic_replay_allowed'] is False

    err = source_state.public_error('LIBRARY_NETWORK_ERROR',
                                    message='fetch https://secret.example /home/alice token=abc')
    assert err['category'] == 'NETWORK' and err['retryable'] is True
    assert 'secret.example' not in err['message'] and '/home/alice' not in err['message']
    assert 'token' not in err['message']

    classified = source_state.classify_error(RuntimeError('password=abc /home/alice'))
    assert classified['category'] == 'INTERNAL'
    assert 'password' not in classified['message'] and 'alice' not in classified['message']


@pytest.mark.parametrize('status,category', [
    ('AUTH_REQUIRED', 'AUTH'), ('FORBIDDEN', 'PERMISSION'),
    ('MAINTENANCE', 'MAINTENANCE'), ('RATE_LIMITED', 'RATE_LIMIT'),
    ('NETWORK_ERROR', 'NETWORK'), ('HTTP_ERROR', 'UPSTREAM_CONTRACT'),
    ('BUSINESS_RESPONSE_INVALID', 'UPSTREAM_CONTRACT'), ('ADAPTER_REQUIRED', 'NOT_IMPLEMENTED'),
    ('InterfaceDriftError', 'UPSTREAM_CONTRACT'),
])
def test_error_translation_separates_caller_actions(status, category):
    translated = source_state.public_error(status)
    assert translated['category'] == category
    assert translated['automatic_replay_allowed'] is False
    assert translated['message']


def test_unknown_enums_stay_unknown_and_only_publish_a_digest():
    value, digest = source_state.canonical_enum('奇怪的学籍状态', {'在籍': 'ACTIVE'})
    assert value == 'UNKNOWN' and digest.startswith('UNKNOWN:')
    assert '奇怪的学籍状态' not in digest
    assert digest == source_state.unknown_digest('奇怪的学籍状态')

    role, digest = domain_models.normalize_role('admin')
    assert role == 'UNKNOWN' and digest and len(digest) == 12
    mapped = domain_models.mapped_status('SUCCESS', {'RESV_EFFECT': 'SCHEDULED'})
    assert mapped == {'status': 'UNKNOWN', 'mapping_state': 'UNKNOWN',
                      'source_status_digest': mapped['source_status_digest']}
    assert 'SUCCESS' != mapped['source_status_digest']


def test_date_only_keeps_its_precision_and_is_never_midnight():
    date_only = domain_models.canonical_time('2026-11-20')
    assert date_only['value'] == '2026-11-20'
    assert date_only['date_precision'] == 'date'

    timed = domain_models.canonical_time('2026-11-20T08:00')
    assert timed['date_precision'] == 'minute' and timed['value'].endswith('+08:00')
    assert timed['inferred_timezone'] is True

    aware = domain_models.canonical_time('2026-11-20T00:00:00Z')
    assert aware['value'] == '2026-11-20T08:00:00+08:00'
    assert aware['inferred_timezone'] is False

    epoch = domain_models.canonical_time('1763600000000')
    assert epoch['date_precision'] == 'millisecond'

    assert domain_models.canonical_time('2026-02-30')['valid'] is False
    assert domain_models.sort_key('2026-11-20') < domain_models.sort_key('2026-11-20T08:00')
    assert domain_models.sort_key('2026-11-20')[1] == 0
    assert 'T00:00' not in domain_models.canonical_time('2026-11-20')['value']
    assert domain_models.calendar_time('2026-11-20') == {
        'kind': 'DATE', 'date': '2026-11-20', 'timezone': 'Asia/Shanghai'}
    with pytest.raises(domain_models.RecordError):
        domain_models.calendar_time('2026-11-20T18:00', '2026-11-20T17:00')


def test_same_title_different_source_ids_are_both_kept_with_a_relation():
    rows = [
        {'course_id': 'SYN-A', 'course_name': '同名课程', 'role': 'student', 'teacher_names': ['甲']},
        {'course_id': 'SYN-B', 'course_name': '同名课程', 'role': 'Teacher', 'teacherName': '乙'},
    ]
    result = domain_models.normalize_records(
        rows, domain_models.normalize_course, source_id='learn.courses',
        semester_id='2026-2027-1', observed_at='2026-11-16T12:00:00+00:00',
        source_evidence_id='learn.courses')
    assert result['rejected_count'] == 0
    assert [record['role'] for record in result['records']] == ['STUDENT', 'TEACHER']
    grouped = domain_models.deduplicate(result['records'])
    assert len(grouped['records']) == 2
    assert len({record['course_uid'] for record in grouped['records']}) == 2
    assert all(record['dedupe_state'] == 'SAME_TITLE_DIFFERENT_IDENTITY'
               for record in grouped['records'])
    assert len(grouped['relations']) == 1
    relation = grouped['relations'][0]
    assert relation['kind'] == 'POSSIBLE_DUPLICATE_TITLE'
    assert len(relation['members']) == 2
    assert 'SAME_TITLE_DIFFERENT_IDENTITY' in grouped['warnings']


def test_missing_identity_and_conflicting_records_fail_closed_and_stay_visible():
    missing = domain_models.normalize_records(
        [{'course_name': '无编号课程', 'role': 'student'}],
        domain_models.normalize_course, source_id='learn.courses',
        semester_id='2026-2027-1', observed_at='2026-11-16T12:00:00+00:00',
        source_evidence_id='learn.courses')['records'][0]
    assert missing['course_uid'] is None
    assert missing['identity_state'] == 'MISSING_STABLE_ID'
    assert 'MISSING_STABLE_ID' in missing['warnings']

    conflicting = [
        domain_models.normalize_course(
            {'course_id': 'SYN-C', 'course_name': '旧标题', 'role': 'student'},
            source_id='learn.courses', semester_id='2026-2027-1',
            observed_at='2026-11-16T12:00:00+00:00', source_evidence_id='learn.courses'),
        domain_models.normalize_course(
            {'course_id': 'SYN-C', 'course_name': '新标题', 'role': 'student'},
            source_id='learn.courses', semester_id='2026-2027-1',
            observed_at='2026-11-16T12:00:00+00:00', source_evidence_id='learn.courses'),
    ]
    grouped = domain_models.deduplicate(conflicting)
    assert len(grouped['records']) == 2
    assert all(record['identity_state'] == 'CONFLICT' and record['course_uid'] is None
               for record in grouped['records'])
    assert 'SOURCE_RECORD_CONFLICT' in grouped['warnings']


def test_empty_title_is_rejected_not_replaced_or_merged():
    result = domain_models.normalize_records(
        [{'course_id': 'SYN-D', 'course_name': '   '}],
        domain_models.normalize_course, source_id='learn.courses',
        semester_id='2026-2027-1', observed_at='2026-11-16T12:00:00+00:00',
        source_evidence_id='learn.courses')
    assert result['records'] == []
    assert result['rejected_count'] == 1
    assert result['rejected'][0]['code'] == 'MISSING_TITLE'
    assert 'MISSING_TITLE' in result['warnings']


def test_course_normalization_fails_closed_for_invalid_time_and_evidence():
    row = {'course_id': 'SYN-A', 'course_name': '课程', 'role': 'student'}
    with pytest.raises(domain_models.RecordError, match='INVALID_OBSERVED_AT'):
        domain_models.normalize_course(row, source_id='learn.courses', semester_id='2026-2027-1',
                                       observed_at='2026-11-16', source_evidence_id='learn.courses')
    with pytest.raises(domain_models.RecordError, match='MISSING_SOURCE_EVIDENCE_ID'):
        domain_models.normalize_course(row, source_id='learn.courses', semester_id='2026-2027-1',
                                       observed_at='2026-11-16T12:00:00+08:00', source_evidence_id='')
    with pytest.raises(domain_models.RecordError, match='MISSING_SOURCE_EVIDENCE_ID'):
        domain_models.normalize_course(row, source_id='learn.courses', semester_id='2026-2027-1',
                                       observed_at='2026-11-16T12:00:00+08:00', source_evidence_id=None)


def test_course_normalization_uses_title_fallback_and_keeps_safe_role_digest():
    record = domain_models.normalize_course(
        {'course_id': 'SYN-A', 'course_name': '', 'title': '回退标题', 'role': 'admin'},
        source_id='learn.courses', semester_id='2026-2027-1',
        observed_at='2026-11-16T12:00:00+08:00', source_evidence_id='learn.courses')
    assert record['title'] == '回退标题'
    assert record['role'] == 'UNKNOWN'
    assert any(warning.startswith('ROLE_UNKNOWN:') for warning in record['warnings'])
    assert 'admin' not in record['warnings']


def test_course_identity_uses_the_same_truncated_parts_as_the_fields():
    semester = 'S' * 100
    record = domain_models.normalize_course(
        {'course_id': 'X' * 60, 'course_name': '课程', 'role': 'student'},
        source_id='learn.courses', semester_id=semester,
        observed_at='2026-11-16T12:00:00+08:00', source_evidence_id='learn.courses')
    assert record['semester_id'] == 'S' * 80
    assert record['semester_id'] in record['course_uid']
    assert semester not in record['course_uid']

    long_record = domain_models.normalize_course(
        {'course_id': 'X' * 300, 'course_name': '课程', 'role': 'student'},
        source_id='learn.courses', semester_id='2026-2027-1',
        observed_at='2026-11-16T12:00:00+08:00', source_evidence_id='learn.courses')
    assert len(long_record['source_record_id']) == 240
    assert long_record['course_uid'] is None
    assert long_record['identity_state'] == 'MISSING_STABLE_ID'


def test_integer_platform_statuses_map_instead_of_becoming_unknown():
    assert domain_models.mapped_status(128, {128: 'COMPLETED'})['status'] == 'COMPLETED'
    assert domain_models.mapped_status(128, {128: 'COMPLETED'})['mapping_state'] == 'MAPPED'
    assert domain_models.mapped_status('128', {128: 'COMPLETED'})['status'] == 'COMPLETED'
    assert domain_models.mapped_status(True, {True: 'BAD'})['status'] == 'UNKNOWN'


def test_three_records_with_the_same_uid_all_stay_in_conflict():
    def record(title):
        return domain_models.normalize_course(
            {'course_id': 'SYN-C', 'course_name': title, 'role': 'student'},
            source_id='learn.courses', semester_id='2026-2027-1',
            observed_at='2026-11-16T12:00:00+08:00', source_evidence_id='learn.courses')

    grouped = domain_models.deduplicate([record('A'), record('B'), record('C')])
    assert len(grouped['records']) == 3
    assert all(item['identity_state'] == 'CONFLICT' and item['course_uid'] is None
               for item in grouped['records'])


def test_standard_record_keeps_account_and_check_time_without_inventing_them():
    wrapper = domain_models.standard_record(
        source_id='learn.courses', source_record_id='SYN-A', account_scope='account:one',
        checked_at='2026-11-16T12:00:00+08:00', date_precision='second',
        source_status='student', model='course-v1', payload={'course_uid': 'x'})
    assert wrapper['account_scope'] == 'account:one'
    assert wrapper['checked_at'] == '2026-11-16T12:00:00+08:00'
    assert wrapper['date_precision'] == 'second'
    assert wrapper['status_mapping'] == 'SOURCE_VALUE_RETAINED'
    with pytest.raises(domain_models.RecordError, match='INVALID_CHECKED_AT'):
        domain_models.standard_record(
            source_id='learn.courses', source_record_id='SYN-A', account_scope='account:one',
            checked_at='2026-11-16', date_precision='date', model='course-v1', payload={})
    with pytest.raises(domain_models.RecordError, match='MISSING_ACCOUNT_SCOPE'):
        domain_models.standard_record(
            source_id='learn.courses', source_record_id='SYN-A', account_scope='',
            checked_at='2026-11-16T12:00:00+08:00', model='course-v1', payload={})


def test_completeness_triad_never_turns_a_failure_into_empty():
    empty = source_state.completeness('EMPTY', True, True)
    assert empty['complete'] and empty['source_complete'] and empty['snapshot_complete']

    auth = source_state.completeness('AUTH_REQUIRED', False, True)
    assert not auth['complete'] and not auth['source_complete']
    assert 'SOURCE_AUTH_REQUIRED' in auth['partial_reasons']

    partial = source_state.completeness('PARTIAL', True, True, missing_pages=[2])
    assert not partial['complete'] and not partial['source_complete']
    assert partial['missing_pages'] == [2]

    aggregated = source_state.aggregate_completeness([
        {'status': 'OK', 'source_complete': True, 'snapshot_complete': True},
        {'status': 'AUTH_REQUIRED', 'source_complete': False, 'snapshot_complete': True},
    ])
    assert aggregated['complete'] is False and aggregated['source_complete'] is False
    assert aggregated['partial_reasons'] == ['SOURCE_AUTH_REQUIRED']
    assert source_state.aggregate_completeness([])['partial_reasons'] == ['NO_SOURCE']

    page = page_metadata({'status': 'PAGE_READ_VERIFIED', 'httpStatus': 200,
                          'authentication_state': 'LOGIN_REQUIRED', 'url': 'https://learn.tsinghua.edu.cn/',
                          'text': 'login'}, 'library', 'page')
    assert page['status'] == 'AUTH_REQUIRED' and not page['complete']
    assert page['error']['category'] == 'AUTH' and page['error']['code'] == 'AUTH_REQUIRED'
    assert 'items' not in page

    verified_empty = QueryEngine(LearningOwner({'courses': [], 'count': 0})).query(
        'learn', 'courses', {})
    assert verified_empty['status'] == 'EMPTY'
    assert verified_empty['complete'] and verified_empty['source_complete']


def test_envelope_reports_read_scope_and_unknown_total(monkeypatch):
    from thu_assistant import library

    def fake(*args):
        return library.result([{'loan_id': '1'}], library.LOANS,
                              complete=False, warnings=['UPSTREAM_WINDOW_COUNT_MISMATCH'])

    monkeypatch.setattr(library, 'query', fake)
    result = QueryEngine(library_owner()).query('library', 'loans', {})
    assert result['status'] == 'PARTIAL'
    assert result['source_complete'] is False and result['complete'] is False
    assert result['upstream_total'] is None  # the source did not provide a total
    assert result['read_scope']['kind'] == 'FULL_LIST'
    assert result['read_scope']['pages_read'] == 1
    assert result['query_time'] and result['read_scope']['query_time'] == result['query_time']
    assert 'SOURCE_PARTIAL' in result['partial_reasons']


def test_cursor_keeps_the_original_window_across_midnight(monkeypatch):
    from thu_assistant import library
    monkeypatch.setattr(library, 'query',
                        lambda *args: library.result([{'loan_id': '1'}, {'loan_id': '2'}], library.LOANS))
    engine = QueryEngine(library_owner())
    clock = [1000.0]
    engine.snapshots = Snapshots(clock=lambda: clock[0])
    first = engine.query('library', 'loans', {}, page_size=1)
    token = first['next_cursor']
    assert token.startswith('c1.')
    original = (first['checked_at'], first['query_time'], deepcopy(first['read_scope']))
    clock[0] += 120  # next day in wall-clock terms; the snapshot clock is unchanged
    second = engine.query('library', 'loans', {}, cursor=token, page_size=1)
    assert (second['checked_at'], second['query_time'], second['read_scope']) == original
    assert first['snapshot_complete'] is False and first['complete'] is False
    assert second['snapshot_complete'] is True and second['complete'] is True


def test_old_cursor_after_parameter_account_or_version_change_is_rejected(monkeypatch):
    from thu_assistant import spaces
    monkeypatch.setattr(spaces, 'query',
                        lambda *args: spaces.result([{'id': '1'}, {'id': '2'}], spaces.SEAT))
    account = {'account_id': 'one'}
    engine = QueryEngine(SimpleNamespace(bridge=lambda *a, **k: account, browsers=None))
    parameters = {'system': 'seat', 'page': 1}
    first = engine.query('library', 'reservations', parameters, page_size=1)
    token = first['next_cursor']

    with pytest.raises(CampusError, match='CURSOR_QUERY_MISMATCH'):
        engine.query('library', 'reservations', {**parameters, 'page': 2}, cursor=token, page_size=1)
    with pytest.raises(CampusError, match='CURSOR_QUERY_MISMATCH'):
        engine.query('library', 'reservations', parameters, cursor=token, page_size=2)
    account['account_id'] = 'two'
    with pytest.raises(CampusError, match='CURSOR_QUERY_MISMATCH'):
        engine.query('library', 'reservations', parameters, cursor=token, page_size=1)

    account['account_id'] = 'one'
    second = engine.query('library', 'reservations', parameters, page_size=1)
    stale = 'c9.' + second['next_cursor'].split('.', 1)[1]
    with pytest.raises(CampusError, match='STALE_SNAPSHOT_TAKE_NEW_SNAPSHOT'):
        engine.query('library', 'reservations', parameters, cursor=stale, page_size=1)

    sid = engine.snapshots.cursors[second['next_cursor']][0]
    engine.snapshots.entries[sid]['version'] = 'snapshot-v1'
    with pytest.raises(CampusError, match='STALE_SNAPSHOT_TAKE_NEW_SNAPSHOT'):
        engine.query('library', 'reservations', parameters, cursor=second['next_cursor'], page_size=1)

    engine.snapshots.drop(sid)
    with pytest.raises(CampusError, match='CURSOR_EXPIRED'):
        engine.query('library', 'reservations', parameters, cursor=second['next_cursor'], page_size=1)


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is not installed on this platform')
def test_cancellation_keeps_the_first_segment_but_marks_the_source_partial(tmp_path):
    module = (ROOT / 'src' / 'thu_assistant' / 'pagination.js').as_posix()
    script = f"""
const {{collectPages}} = require({json.dumps(module)});
(async () => {{
  let calls = 0;
  const flag = await collectPages(6, async p => {{ calls++; return [{{yyfwid:'a'}},{{yyfwid:'b'}}]; }},
    {{limit:5, cancel:()=>calls>=1}});
  let calls2 = 0;
  const thrown = await collectPages(6, async p => {{
    calls2++; if (calls2 === 2) throw Object.assign(new Error('synthetic'), {{code:'CANCELLED'}});
    return [{{yyfwid:'a'}},{{yyfwid:'b'}}];
  }}, {{limit:5}});
  const out = v => ({{items:v.items.length, complete:v.complete, cancelled:v.cancelled,
    snapshot_complete:v.snapshot_complete, warnings:v.warnings, pages_read:v.pages_read}});
  console.log(JSON.stringify({{flag:out(flag), thrown:out(thrown)}}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    env = {**os.environ, 'LOCALAPPDATA': str(tmp_path)}
    completed = subprocess.run([shutil.which('node'), '-e', script], capture_output=True,
                               text=True, env=env, timeout=30)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    for case in ('flag', 'thrown'):
        assert result[case] == {'items': 2, 'complete': False, 'cancelled': True,
                                'snapshot_complete': False, 'pages_read': 1,
                                'warnings': ['READ_CANCELLED']}


def test_cache_policy_keeps_availability_live_and_requires_explicit_history_fallback():
    account, params, source = 'one', {}, 'library.seat_availability'
    entry = cache_policy.record(account=account, parameters=params, source=source,
                                checked_at='2026-11-16T12:00:00+08:00', value=[{'seat': 'A'}])
    live = cache_policy.evaluate(entry, resource_kind='library.seat_availability',
                                 account=account, parameters=params, source=source)
    assert not live['allowed'] and live['cache_state'] == 'LIVE_ONLY_RESOURCE'
    assert live['reusable_for_booking'] is False and live['proves_authentication'] is False

    directory = cache_policy.record(account='PUBLIC', parameters={}, source='info.directory',
                                    checked_at='2026-11-16T12:00:00+08:00', value=[])
    fresh = cache_policy.evaluate(directory, resource_kind='info.directory', account='PUBLIC',
                                  parameters={}, source='info.directory',
                                  now=datetime(2026, 11, 16, 12, 30, tzinfo=TZ))
    assert fresh['allowed'] and fresh['freshness'] == 'CACHED'
    assert fresh['proves_authentication'] is False and fresh['reusable_for_booking'] is False

    stale = cache_policy.evaluate(directory, resource_kind='info.directory', account='PUBLIC',
                                  parameters={}, source='info.directory',
                                  now=datetime(2026, 11, 16, 14, 0, tzinfo=TZ))
    assert not stale['allowed'] and stale['cache_state'] == 'STALE'

    mismatched = cache_policy.evaluate(directory, resource_kind='info.directory', account='OTHER',
                                       parameters={}, source='info.directory',
                                       now=datetime(2026, 11, 16, 12, 30, tzinfo=TZ))
    assert not mismatched['allowed'] and mismatched['cache_state'] == 'BOUND_MISMATCH'

    history = cache_policy.record(account='one', parameters={}, source='library.loans',
                                  checked_at='2026-10-01T12:00:00+08:00', value=[])
    rejected = cache_policy.evaluate(history, resource_kind='library.loans', account='one',
                                     parameters={}, source='library.loans',
                                     now=datetime(2026, 11, 16, 12, 0, tzinfo=TZ))
    assert not rejected['allowed'] and rejected['cache_state'] == 'STALE'
    allowed = cache_policy.evaluate(history, resource_kind='library.loans', account='one',
                                    parameters={}, source='library.loans',
                                    now=datetime(2026, 11, 16, 12, 0, tzinfo=TZ), allow_stale=True)
    assert allowed['allowed'] and allowed['freshness'] == 'HISTORICAL'
    assert allowed['complete'] is False and allowed['source_complete'] is False
    assert allowed['reusable_for_booking'] is False and allowed['proves_authentication'] is False


def test_directory_cache_is_reported_as_cached_and_never_complete_when_stale(tmp_path, monkeypatch):
    path = tmp_path / 'directory.json'
    monkeypatch.setattr(catalog, 'directory_path', lambda: path)
    record = {**cache_policy.record(account='PUBLIC', parameters={}, source='info.directory',
                                    checked_at=source_state.now_iso(), value=[]),
              'query_schema': 1, 'complete': True, 'services': [], 'expected': 0,
              'pages': [], 'missing_pages': [], 'checkedAt': source_state.now_iso()}
    path.write_text(json.dumps(record), encoding='utf-8')
    fresh = catalog.list_services()
    assert fresh['complete'] is True and fresh['verification_level'] == 'DIRECTORY_CACHE'
    assert fresh['source']['freshness'] == 'CACHED'
    assert fresh['source']['proves_authentication'] is False

    record['checked_at'] = '2026-01-01T00:00:00+08:00'
    path.write_text(json.dumps(record), encoding='utf-8')
    tampered = catalog.list_services()
    assert tampered['complete'] is False and 'DIRECTORY_CACHE_BINDING_MISMATCH' in tampered['warnings']

    stale_record = {**cache_policy.record(account='PUBLIC', parameters={}, source='info.directory',
                                          checked_at='2026-01-01T00:00:00+08:00', value=[]),
                    'query_schema': 1, 'complete': True, 'services': [], 'expected': 0,
                    'pages': [], 'missing_pages': [], 'checkedAt': '2026-01-01T00:00:00+08:00'}
    path.write_text(json.dumps(stale_record), encoding='utf-8')
    stale = catalog.list_services()
    assert stale['complete'] is False and 'DIRECTORY_CACHE_STALE' in stale['warnings']
    assert stale['partial_reasons'] == ['DIRECTORY_CACHE_STALE']


def test_result_envelope_schema_accepts_v020_envelopes_and_errors():
    empty = QueryEngine(LearningOwner({'courses': [], 'count': 0})).query('learn', 'courses', {})
    validate('result-envelope.schema.json', empty)
    assert empty['status'] == 'EMPTY' and 'error' not in empty

    raw = {'status': 'AUTH_REQUIRED', 'httpStatus': 401,
           'url': 'https://learn.tsinghua.edu.cn/', 'text': 'login', 'checked_at': None}
    auth = QueryEngine(SimpleNamespace(bridge=lambda *a, **k: deepcopy(raw), browsers=None)).query(
        'library', 'page', {})
    assert auth['status'] == 'AUTH_REQUIRED' and auth['error']['category'] == 'AUTH'
    validate('result-envelope.schema.json', auth)


def test_source_evidence_builder_conforms_and_degrades_authentication():
    scope = {'kind': 'FULL_LIST', 'offset': 0, 'limit': 200, 'pages_read': 2,
             'expected_pages': 2, 'query_time': '2026-11-16T12:00:00+08:00', 'window': None}
    evidence = source_state.source_evidence(
        source_id='learn.assignments', service='learn', operation='assignments',
        adapter_version='learn-read-v1', checked_at='2026-11-16T12:00:00+08:00',
        state='OK', scope=scope, row_count=2, upstream_total=2, source_complete=True,
        complete=True, verification_level='BUSINESS_READ', synthetic=True)
    validate('source-evidence.schema.json', evidence)

    expired = source_state.source_evidence(
        source_id='learn.assignments', service='learn', operation='assignments',
        adapter_version='learn-read-v1', checked_at='2026-11-16T12:00:00+08:00',
        state='EMPTY', scope=scope, row_count=0, upstream_total=0, source_complete=True,
        complete=True, auth_state='EXPIRED', verification_level='NONE', synthetic=True)
    assert expired['state'] == 'AUTH_REQUIRED'
    assert expired['complete'] is False and expired['source_complete'] is False
    validate('source-evidence.schema.json', expired)

    unknown_total = source_state.source_evidence(
        source_id='library.catalog_search', service='library', operation='catalog_search',
        adapter_version='library-read-v1', checked_at='2026-11-16T12:00:00+08:00',
        state='PARTIAL', scope=scope, row_count=20, upstream_total=0, source_complete=False,
        complete=False, warnings=['UPSTREAM_TOTAL_UNKNOWN'], verification_level='BUSINESS_READ',
        synthetic=True)
    assert unknown_total['upstream_total'] is None
    validate('source-evidence.schema.json', unknown_total)


def test_synthetic_query_fixtures_drive_the_same_normalization_rules():
    fixture = ROOT / 'tests' / 'fixtures' / 'query' / 'courses.json'
    rows = json.loads(fixture.read_text(encoding='utf-8'))['rows']
    result = domain_models.normalize_records(
        rows, domain_models.normalize_course, source_id='learn.courses',
        semester_id='2026-2027-1', observed_at='2026-11-16T12:00:00+08:00',
        source_evidence_id='learn.courses')
    assert result['rejected_count'] == 1
    assert result['rejected'][0]['code'] == 'MISSING_TITLE'
    grouped = domain_models.deduplicate(result['records'])
    assert len(grouped['records']) == 3  # two same-title courses plus the unnumbered one
    for record in grouped['records']:
        validate('sources/data-models/course.schema.json', record)
    assert len(grouped['relations']) == 1

    times = json.loads((ROOT / 'tests' / 'fixtures' / 'query' / 'times.json')
                       .read_text(encoding='utf-8'))['values']
    assert domain_models.canonical_time(times['date_only'])['date_precision'] == 'date'
    assert domain_models.canonical_time(times['platform_local'])['inferred_timezone'] is True
    assert domain_models.canonical_time(times['offset_aware'])['value'] == '2026-11-20T08:00:00+08:00'
    assert domain_models.canonical_time(times['epoch_milliseconds'])['date_precision'] == 'millisecond'
    assert domain_models.canonical_time(times['unknown'])['valid'] is False


def test_learn_and_generic_page_cursors_are_bound_to_the_account():
    raw = {'items': [{'id': 1}, {'id': 2}, {'id': 3}], 'complete': True,
           'checked_at': '2026-11-16T12:00:00+08:00', 'warnings': [], 'pages': []}
    owner = LearningOwner(raw, account='A')
    engine = QueryEngine(owner)
    first = engine.query('learn', 'assignments', {'course_id': 'X'}, page_size=1)
    owner.account = 'B'
    with pytest.raises(CampusError, match='CURSOR_QUERY_MISMATCH'):
        engine.query('learn', 'assignments', {'course_id': 'X'},
                     cursor=first['next_cursor'], page_size=1)

    page = {'status': 'PAGE_READ_VERIFIED', 'httpStatus': 200,
            'url': 'https://lib.tsinghua.edu.cn/article', 'text': 'abcdef', 'checked_at': 'fixed'}
    account = {'id': 'A'}

    def bridge(command, **kwargs):
        if command == 'action_identity':
            return {'account_id': account['id']}
        return deepcopy(page)

    page_engine = QueryEngine(SimpleNamespace(bridge=bridge, browsers=None))
    args = {'url': 'https://lib.tsinghua.edu.cn/article'}
    first_page = page_engine.read_page({**args, 'limit': 2})
    account['id'] = 'B'
    with pytest.raises(CampusError, match='CURSOR_QUERY_MISMATCH'):
        page_engine.read_page({**args, 'limit': 2, 'cursor': first_page['next_cursor']})


def test_cache_binding_rejects_a_changed_checked_at():
    account, params, source = 'one', {}, 'info.directory'
    entry = cache_policy.record(account=account, parameters=params, source=source,
                                checked_at='2026-01-01T00:00:00+08:00', value=[])
    entry['checked_at'] = '2026-11-16T12:00:00+08:00'
    verdict = cache_policy.evaluate(entry, resource_kind='info.directory', account=account,
                                    parameters=params, source=source,
                                    now=datetime(2026, 11, 16, 12, 1, tzinfo=TZ))
    assert verdict['allowed'] is False and verdict['cache_state'] == 'BOUND_MISMATCH'
    with pytest.raises(ValueError, match='INVALID_CACHE_CHECKED_AT'):
        cache_policy.record(account=account, parameters=params, source=source,
                            checked_at='2026-01-01', value=[])


def test_missing_page_encodings_cannot_be_silently_dropped():
    numeric = source_state.completeness('OK', True, True, missing_pages=['2'])
    assert numeric['complete'] is False and numeric['missing_pages'] == [2]
    invalid = source_state.completeness('OK', True, True, missing_pages=[True])
    assert invalid['complete'] is False
    assert 'INVALID_MISSING_PAGES' in invalid['partial_reasons']
    invalid_text = source_state.completeness('OK', True, True, missing_pages=['two'])
    assert invalid_text['complete'] is False
    assert 'INVALID_MISSING_PAGES' in invalid_text['partial_reasons']


def test_read_scope_uses_the_real_window_for_page_based_operations():
    scope = read_scope_for('library', 'reservations',
                           {'system': 'seat', 'page': 2, 'limit': 10},
                           [], {'complete': True}, '2026-11-16T12:00:00+08:00')
    assert scope['offset'] == 10 and scope['limit'] == 10
    scope = read_scope_for('library', 'reservations',
                           {'system': 'seat', 'page': 2},
                           [], {'complete': True, 'limit': 25}, '2026-11-16T12:00:00+08:00')
    assert scope['offset'] == 25 and scope['limit'] == 25


def test_source_evidence_sanitizes_url_and_validates_public_fields():
    scope = {'kind': 'FULL_LIST', 'offset': 0, 'limit': 10, 'pages_read': 1,
             'expected_pages': 1, 'query_time': 'fixed', 'window': None}
    evidence = source_state.source_evidence(
        source_id='learn.assignments', service='learn', operation='assignments',
        adapter_version='v1', checked_at='2026-11-16T12:00:00+08:00',
        state='OK', scope=scope, row_count=1, upstream_total=1,
        source_complete=True, complete=True, verification_level='BUSINESS_READ',
        url='https://user:pass@learn.tsinghua.edu.cn/page?ticket=SECRET&q=ok#frag')
    assert 'SECRET' not in evidence['url'] and 'user' not in evidence['url']
    assert evidence['url'].endswith('?q=ok')
    assert evidence['read_scope']['query_time'] != 'fixed'
    for field, value in [('operation', ''), ('adapter_version', ''),
                         ('verification_level', 'BOGUS')]:
        kwargs = dict(source_id='learn.assignments', service='learn', operation='assignments',
                      adapter_version='v1', checked_at='2026-11-16T12:00:00+08:00',
                      state='OK', scope=scope, source_complete=True, complete=True,
                      verification_level='BUSINESS_READ')
        kwargs[field] = value
        with pytest.raises(ValueError):
            source_state.source_evidence(**kwargs)


def test_filtered_empty_directory_is_not_reported_as_an_empty_source():
    raw = {'services': [{'id': 'other', 'name': '其他服务'}], 'complete': True,
           'expected': 1, 'count': 1, 'rowCount': 1,
           'pages': [{'page': 1, 'count': 1}], 'missing_pages': [], 'warnings': [],
           'checkedAt': '2026-11-16T12:00:00+08:00'}
    engine = QueryEngine(SimpleNamespace(bridge=lambda *a, **k: deepcopy(raw), browsers=None))
    result = engine.query('info', 'directory', {'query': 'no-such-service'})
    assert result['status'] == 'OK' and result['complete'] is True
    assert result['items'] == [] and result['matched_items'] == 0
    assert result['total_scope'] == 'filtered_result' and result['upstream_total'] == 1


def test_corrupt_directory_cache_degrades_to_static_catalog(tmp_path, monkeypatch):
    path = tmp_path / 'directory.json'
    monkeypatch.setattr(catalog, 'directory_path', lambda: path)
    path.write_text('[]', encoding='utf-8')
    result = catalog.list_services()
    assert result['count'] >= 0 and result['complete'] is False
    assert 'DIRECTORY_CACHE_FORMAT_INVALID' in result['warnings']
    path.unlink()
    missing = catalog.list_services()
    assert missing['complete'] is False
    assert missing['partial_reasons'] == ['DIRECTORY_CACHE_MISSING']


def test_describe_exposes_cursor_versions_and_the_cache_policy():
    described = describe('library')
    assert described['cursor_version'] == 'c1'
    assert described['snapshot_version'] == 'snapshot-v2'
    assert described['completeness']['fields'] == ['source_complete', 'snapshot_complete', 'complete']
    assert described['cache_policy']['policies']['availability']['label'] == 'LIVE_ONLY'
