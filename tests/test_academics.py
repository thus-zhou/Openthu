"""Module 4.1 acceptance tests: timetable, exams and course grades.

All records are synthetic.  No campus, browser or model request is made; the
only "transport" is a local fixture returning rows that clearly declare
``synthetic=True``.  The production entry point is asserted to stay blocked.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from thu_assistant import academics, domain_models, queries
from thu_assistant.academics import (
    CAPABILITIES, AcademicsReader, CourseIndex, PeriodTable, SchoolCalendar,
    calendar_events, expand, merge_records, normalize_exam, normalize_grade,
    normalize_semester, normalize_timetable_entry, normalize_week_pattern,
    select_semester,
)
from thu_assistant.academics.errors import CODES, AcademicsError
from thu_assistant.academics import events as academics_events
from thu_assistant.academics import rules as academics_rules
from thu_assistant.academics import sensitivity

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / 'contracts' / 'v0.2'
FIXTURES = ROOT / 'tests' / 'fixtures' / 'academics'
CHECKED = '2026-11-17T09:00:00+08:00'
SEMESTER = '2026-2027-1'


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def contract(name):
    return json.loads((CONTRACTS / name).read_text(encoding='utf-8'))


def validate_contract(name, value):
    jsonschema.Draft202012Validator(contract(name)).validate(value)


def semester_rows():
    return fixture('semesters.json')['semesters']


def course_rows():
    return fixture('courses.json')['courses']


def timetable_rows():
    return fixture('timetable.json')['entries']


def exam_rows():
    return fixture('exams.json')['exams']


def grade_rows():
    return fixture('grades.json')['grades']


def row(rows, key, value):
    for item in rows:
        if item.get(key) == value:
            return item
    raise AssertionError(f'missing fixture row {key}={value}')


def calendar():
    return SchoolCalendar.from_mapping(fixture('calendar.json')['calendar'])


def period_table():
    return PeriodTable.from_mapping(fixture('periods.json')['period_table'])


def course_index():
    return CourseIndex(course_rows(), observed_at=CHECKED,
                       source_evidence_id='learn.courses')


def entry(entry_id):
    return normalize_timetable_entry(
        row(timetable_rows(), 'entry_id', entry_id), semester_id=SEMESTER,
        observed_at=CHECKED, source_evidence_id='academics.timetable',
        course_index=course_index(), source_id='academics.timetable')


def exam(exam_id):
    return normalize_exam(
        row(exam_rows(), 'exam_id', exam_id), semester_id=SEMESTER,
        observed_at=CHECKED, source_evidence_id='academics.exams',
        course_index=course_index(), source_id='academics.exams')


def grade(grade_id):
    return normalize_grade(
        row(grade_rows(), 'grade_id', grade_id), semester_id=SEMESTER,
        observed_at=CHECKED, source_evidence_id='academics.grades',
        course_index=course_index(), source_id='academics.grades')


def expansion(entry_id, *, with_periods=True, with_calendar=True):
    item = entry(entry_id)
    return expand(item['week_pattern'], calendar() if with_calendar else None,
                  period_table() if with_periods else None, semester_id=SEMESTER,
                  location=item['location'])


def synthetic_transport(pages, *, coverage=None, semesters=None, courses=None,
                        errors=(), upstream_total=None, url=None):
    def transport(capability, parameters):
        return {
            'synthetic': True,
            'checked_at': CHECKED,
            'adapter_version': 'academics-synthetic-v1',
            'semesters': semester_rows() if semesters is None else semesters,
            'courses': course_rows() if courses is None else courses,
            'pages': pages,
            'errors': list(errors),
            'coverage': {'declared': True, 'complete': True} if coverage is None else coverage,
            'upstream_total': upstream_total,
            'url': url,
        }
    transport.synthetic = True
    transport.adapter_version = 'academics-synthetic-v1'
    return transport


def rehearsal(capability, rows, **transport_options):
    transport = synthetic_transport([{'number': 1, 'rows': rows}], **transport_options)
    options = {'semester': SEMESTER}
    if capability == 'grades':
        options['explicit_sensitive'] = True
    return AcademicsReader().read_synthetic(capability, transport, **options)


# ---------------------------------------------------------------- contracts

def test_capability_table_matches_the_frozen_readiness_contract():
    readiness = json.loads((CONTRACTS / 'sources' / 'adapter-readiness.json').read_text(encoding='utf-8'))
    frozen = {item['module']: item for item in readiness['modules']}['4.1']
    assert set(CAPABILITIES) == {'timetable', 'exams', 'grades'}
    for name, spec in CAPABILITIES.items():
        source = 'academics.' + name
        assert source in frozen['source_ids']
        assert spec['start_conclusion'] == frozen['start_conclusion'] == 'BLOCKED_SOURCE_EVIDENCE'
        assert spec['implementation_state'] == 'BLOCKED'
        assert spec['runtime_availability'] == 'UNAVAILABLE'
        assert spec['blocked_scope']
    assert sorted(scoped for spec in CAPABILITIES.values() for scoped in spec['blocked_scope']) == sorted(frozen['blocked_scope'])


def test_source_registry_keeps_academics_unverified():
    registry = json.loads((CONTRACTS / 'sources' / 'source-registry.json').read_text(encoding='utf-8'))
    rows = {item['source_id']: item for item in registry['sources']}
    for name in CAPABILITIES:
        item = rows['academics.' + name]
        assert item['evidence_level'] == 'NONE'
        assert item['runtime_availability'] == 'UNAVAILABLE'
        assert item['implementation_state'] == 'BLOCKED'


def test_academics_errors_use_the_frozen_category_policy():
    frozen = contract('error-classification.json')
    for code, (category, retryable, action, message) in CODES.items():
        assert code.isupper() and len(code) <= 81
        assert category in frozen['categories']
        assert frozen['categories'][category]['retryable'] is retryable
        assert frozen['categories'][category]['user_action'] == action
        assert 0 < len(message) <= 200
    error = AcademicsError('ACADEMICS_GRADE_NOT_EVENT').as_error()
    assert error['category'] == 'INPUT' and error['automatic_replay_allowed'] is False


def test_operation_schemas_accept_only_registered_parameters():
    for name, schema in academics.operation_schemas().items():
        assert schema['additionalProperties'] is False
        assert set(schema['properties']) == {'semester', 'course', 'scope', 'limit'}
        assert schema['category'] == 'read'
        assert 'url' not in json.dumps(schema)
        assert 'cookie' not in json.dumps(schema).lower()


def test_describe_reports_blocked_capabilities_and_rule_versions():
    described = academics.describe()
    assert described['live_reads_enabled'] is False
    assert all(item['implementation_state'] == 'BLOCKED'
               for item in described['capabilities'].values())
    assert described['rules']['week_expansion'] == academics_rules.EXPANSION_RULE
    assert described['grade_privacy']['policy']['public_dashboard'] == 'EXCLUDED'


# ---------------------------------------------------------------- semesters

def test_semester_selection_uses_the_source_id():
    records = [normalize_semester(item) for item in semester_rows()]
    selected = select_semester(records, SEMESTER)
    assert selected['state'] == 'SELECTED'
    assert selected['basis'] == 'SOURCE_ID'
    assert selected['semester']['title'] == '2026-2027 秋季学期'


def test_equal_titles_with_different_ids_are_not_merged():
    records = [normalize_semester(item) for item in semester_rows()]
    titles = [item['title'] for item in records]
    assert titles.count('2026-2027 秋季学期') == 2
    assert len({item['semester_id'] for item in records}) == 3
    selected = select_semester(records, '2026-2027 秋季学期')
    assert selected['state'] == 'AMBIGUOUS'
    assert selected['semester'] is None
    assert set(selected['candidates']) == {'2026-2027-1', '2025-2026-1'}


def test_unknown_semester_request_is_missing_not_guessed():
    records = [normalize_semester(item) for item in semester_rows()]
    selected = select_semester(records, '2027-2028-1')
    assert selected['state'] == 'MISSING'
    assert selected['semester'] is None
    assert 'SEMESTER_NOT_FOUND' in selected['warnings']


def test_title_fallback_requires_an_explicit_opt_in():
    records = [normalize_semester(item) for item in semester_rows()]
    unique_only = [item for item in records if item['semester_id'] != '2025-2026-1']
    selected = select_semester(unique_only, '2026-2027 秋季学期')
    assert selected['state'] == 'AMBIGUOUS'
    assert selected['semester'] is None


def test_multiple_current_semesters_are_ambiguous():
    records = [normalize_semester(item) for item in semester_rows()]
    records[1]['current'] = True
    selected = select_semester(records)
    assert selected['state'] == 'AMBIGUOUS'
    assert 'MULTIPLE_CURRENT_SEMESTERS' in selected['warnings']


# ------------------------------------------------------- course association

def test_course_index_keeps_same_title_across_semesters():
    index = course_index()
    assert len(index.deduped['records']) == 6
    relations = index.deduped['relations']
    titles = {relation['title'] for relation in relations}
    assert '合成数据结构' in titles
    assert '合成操作系统' in titles
    assert all(relation['semantics'] == 'SAME_TITLE_DIFFERENT_IDENTITY' for relation in relations)


def test_teaching_classes_with_the_same_title_stay_distinct():
    index = course_index()
    first = index.associate(course_id='SYNTH-COURSE-A', semester_id=SEMESTER)
    second = index.associate(course_id='SYNTH-COURSE-A2', semester_id=SEMESTER)
    assert first['association_state'] == 'EXACT_ID'
    assert second['association_state'] == 'EXACT_ID'
    assert first['course_uid'] != second['course_uid']
    by_class = index.associate(class_id='CLASS-02', semester_id=SEMESTER)
    assert by_class['association_state'] == 'EXACT_CLASS_ID'
    assert by_class['course_uid'] == second['course_uid']


def test_title_only_association_never_produces_a_course_uid():
    index = course_index()
    ambiguous = index.associate(title='合成同名课程', semester_id=SEMESTER)
    assert ambiguous['association_state'] == 'AMBIGUOUS_TITLE'
    assert ambiguous['course_uid'] is None
    assert len(ambiguous['candidates']) == 2
    unique = index.associate(title='合成操作系统', semester_id='2026-2027-2')
    assert unique['association_state'] == 'TITLE_ONLY_UNIQUE'
    assert unique['course_uid'] is None
    assert 'COURSE_ASSOCIATION_UNVERIFIED' in unique['warnings']


def test_course_id_mismatch_keeps_course_identity_with_a_warning():
    item = entry('TT-9')
    assert item['course_uid'].endswith('SYNTH-COURSE-A')
    assert item['title'] == '合成错误标题'
    assert 'COURSE_TITLE_MISMATCH' in item['warnings']


def test_conflicting_duplicate_entry_is_kept_twice():
    first = entry('TT-1')
    second = dict(first)
    second['location'] = '合成教学楼 999'
    merged = merge_records([first, second])
    assert len(merged['records']) == 2
    assert all(item['identity_state'] == 'CONFLICT' for item in merged['records'])
    assert all(item['entry_uid'] is None for item in merged['records'])
    assert 'SOURCE_RECORD_CONFLICT' in merged['warnings']


# ---------------------------------------------------------- week expansion

def test_all_weeks_expand_with_calendar_and_period_table():
    result = expansion('TT-1')
    assert result['state'] == 'EXPANDED'
    dates = [item['date'] for item in result['occurrences']]
    assert dates[0] == '2026-09-14'
    assert dates[-1] == '2026-12-28'
    assert len(dates) == 16
    first = result['occurrences'][0]
    assert first['start'] == '08:00' and first['end'] == '09:35'
    assert first['time']['kind'] == 'DATE_TIME'
    assert first['time']['start'] == '2026-09-14T08:00:00+08:00'
    assert first['time_state'] == 'PERIOD_TABLE'


def test_odd_and_even_weeks_expand_only_their_weeks():
    odd = expansion('TT-3')
    even = expansion('TT-4')
    assert [item['week'] for item in odd['occurrences']] == [1, 3, 5, 7, 9, 11, 13, 15]
    assert [item['week'] for item in even['occurrences']] == [2, 4, 6, 8, 10, 12, 14, 16]


def test_holiday_is_skipped_and_makeup_day_replaces_the_suspended_date():
    result = expansion('TT-2')
    dates = [item['date'] for item in result['occurrences']]
    assert '2026-10-01' not in dates          # holiday
    assert '2026-10-08' not in dates          # replaced by the makeup day
    assert '2026-10-10' in dates              # Saturday makeup for Thursday
    assert len(dates) == 15
    assert result['state'] == 'PARTIAL'
    reasons = {item['reason'] for item in result['skipped']}
    assert {'HOLIDAY', 'REPLACED_BY_MAKEUP_DAY'} <= reasons
    assert 'SKIPPED_HOLIDAY' in result['warnings']


def test_cross_week_course_outside_the_calendar_is_not_invented():
    result = expansion('TT-5')
    assert result['occurrences'] == []
    assert result['state'] == 'PARTIAL'
    assert {item['reason'] for item in result['skipped']} == {'OUTSIDE_TEACHING_WEEKS'}
    assert result['pattern']['weeks'] == [17, 18]


def test_missing_calendar_keeps_the_raw_cycle():
    result = expansion('TT-1', with_calendar=False)
    assert result['state'] == 'RAW_CYCLE_ONLY'
    assert result['occurrences'] == []
    assert result['pattern']['weeks'] == list(range(1, 17))
    assert 'CALENDAR_UNAVAILABLE' in result['warnings']


def test_missing_period_table_keeps_dates_without_clock_times():
    result = expansion('TT-1', with_periods=False)
    assert result['state'] == 'EXPANDED_DATE_ONLY'
    assert result['occurrences']
    assert all(item['start'] is None and item['end'] is None for item in result['occurrences'])
    assert all(item['time'] is None for item in result['occurrences'])
    assert all(item['time_state'] == 'PERIOD_TABLE_MISSING' for item in result['occurrences'])
    assert 'PERIOD_TABLE_MISSING' in result['warnings']
    assert result['occurrences'][0]['date'] == '2026-09-14'


def test_unknown_parity_keeps_cycle_incomplete():
    pattern = normalize_week_pattern({'weekday': 1, 'start_week': 1, 'end_week': 16,
                                      'parity': '隔周?', 'periods': [1, 2]})
    assert pattern['complete'] is False
    assert pattern['parity'] == 'UNKNOWN'
    assert 'WEEK_PARITY_UNKNOWN' in pattern['warnings']
    result = expand(pattern, calendar(), period_table(), semester_id=SEMESTER)
    assert result['state'] == 'INCOMPLETE_PATTERN'
    assert result['occurrences'] == []


def test_calendar_semester_mismatch_does_not_expand():
    other = SchoolCalendar('2026-2027-2', '2026-09-14', 16)
    result = expand(entry('TT-1')['week_pattern'], other, period_table(), semester_id=SEMESTER)
    assert result['state'] == 'CALENDAR_MISMATCH'
    assert result['occurrences'] == []
    assert 'SEMESTER_MISMATCH:CALENDAR' in result['warnings']


def test_period_table_rejects_invalid_sections():
    with pytest.raises(AcademicsError):
        PeriodTable('x', [{'period': 1, 'start': '09:00', 'end': '08:00'}])
    with pytest.raises(AcademicsError):
        PeriodTable('x', [{'period': 1, 'start': '08:00', 'end': '08:45'},
                          {'period': 1, 'start': '09:00', 'end': '09:45'}])
    with pytest.raises(AcademicsError):
        PeriodTable('x', [{'period': 1, 'start': '8:00', 'end': '08:45'}])


def test_expansion_derivation_carries_the_rule_version():
    result = expansion('TT-1')
    derivation = result['derivation']
    assert derivation['kind'] == 'DERIVED'
    assert derivation['rule_version'] == academics_rules.EXPANSION_RULE
    assert derivation['source_of_dates'] == academics_rules.CALENDAR_VERSION
    assert derivation['source_of_times'] == academics_rules.PERIOD_TABLE_VERSION
    assert derivation['not_a_source_fact'] is True


# ------------------------------------------------------------------ exams

def test_date_only_exam_has_no_clock_time_and_no_midnight():
    item = exam('EX-1')
    assert item['time_precision'] == 'date'
    assert item['date'] == '2027-01-05'
    assert item['start_time'] is None and item['end_time'] is None
    event = academics_events.exam_event(item)
    assert event['time'] == {'kind': 'DATE', 'date': '2027-01-05', 'timezone': 'Asia/Shanghai'}


def test_timed_exam_keeps_minute_precision():
    item = exam('EX-2')
    assert item['time_precision'] == 'minute'
    assert (item['start_time'], item['end_time']) == ('09:00', '11:00')
    event = academics_events.exam_event(item)
    assert event['time']['kind'] == 'DATE_TIME'
    assert event['time']['start'] == '2027-01-07T09:00:00+08:00'
    assert event['time']['end'] == '2027-01-07T11:00:00+08:00'
    assert event['status'] == 'CONFIRMED'


def test_cancelled_and_rescheduled_exams_keep_distinct_states():
    cancelled = exam('EX-3')
    rescheduled = exam('EX-4')
    assert cancelled['state'] == 'CANCELLED'
    assert rescheduled['state'] == 'RESCHEDULED'
    assert academics_events.exam_event(cancelled)['status'] == 'CANCELLED'
    assert academics_events.exam_event(rescheduled)['status'] == 'TENTATIVE'


def test_makeup_and_deferred_are_not_rescheduled():
    makeup = exam('EX-5')
    deferred = exam('EX-6')
    assert makeup['state'] == 'MAKEUP'
    assert deferred['state'] == 'DEFERRED'
    assert deferred['date'] is None and deferred['time_precision'] == 'unknown'
    assert makeup['title'] != deferred['title']


def test_unknown_exam_state_stays_unknown_with_a_digest():
    item = exam('EX-7')
    assert item['state'] == 'UNKNOWN'
    assert any(warning.startswith('EXAM_STATE_UNKNOWN:') for warning in item['warnings'])
    assert '奇怪状态' not in json.dumps(item, ensure_ascii=False)
    assert academics_events.exam_event(item)['status'] == 'UNKNOWN'


def test_exam_end_before_start_is_rejected():
    invalid = fixture('invalid-rows.json')['exam_rows']
    codes = []
    for row_value in invalid:
        with pytest.raises(domain_models.RecordError) as caught:
            normalize_exam(row_value, semester_id=SEMESTER, observed_at=CHECKED,
                           source_evidence_id='academics.exams',
                           course_index=course_index(), source_id='academics.exams')
        codes.append(caught.value.code)
    assert codes == ['END_BEFORE_START', 'EXAM_TIME_WITHOUT_DATE']


def test_exam_without_an_id_keeps_no_fabricated_uid():
    item = exam('')
    assert item['exam_uid'] is None
    assert item['identity_state'] == 'MISSING_STABLE_ID'
    assert 'MISSING_STABLE_ID' in item['warnings']
    with pytest.raises(AcademicsError):
        academics_events.exam_event(item)


def test_exam_coverage_undeclared_cannot_claim_all_sessions():
    transport = synthetic_transport([{'number': 1, 'rows': exam_rows()}],
                                     coverage={'declared': False, 'complete': False})
    result = AcademicsReader().read_synthetic('exams', transport, semester=SEMESTER)
    assert result['status'] == 'PARTIAL'
    assert result['complete'] is False
    assert 'EXAM_COVERAGE_NOT_DECLARED' in result['warnings']
    assert result['items']


# ----------------------------------------------------------------- grades

def test_numeric_grade_is_graded():
    item = grade('G-1')
    assert item['grade_value'] == 92
    assert item['grade_scale'] == 'NUMERIC_100'
    assert item['state'] == 'GRADED'
    assert item['credits'] == 3


def test_zero_grade_is_not_ungraded():
    item = grade('G-2')
    assert item['grade_value'] == 0
    assert item['state'] == 'GRADED'
    assert item['grade_value'] is not None


def test_level_and_pass_fail_grades_stay_textual():
    level = grade('G-3')
    pass_fail = grade('G-4')
    assert (level['grade_text'], level['grade_scale'], level['grade_value']) == ('A', 'GRADE_LEVEL', None)
    assert (pass_fail['grade_text'], pass_fail['grade_scale']) == ('P', 'PASS_FAIL')


def test_ungraded_and_withheld_are_separate():
    ungraded = grade('G-5')
    withheld = grade('G-6')
    assert ungraded['state'] == 'UNGRADED'
    assert ungraded['grade_value'] is None and ungraded['grade_text'] is None
    assert 'GRADE_NOT_PUBLISHED' in ungraded['warnings']
    assert withheld['state'] == 'WITHHELD'
    assert withheld['grade_value'] is None and withheld['grade_text'] is None
    assert ungraded['state'] != withheld['state']


def test_unknown_scale_keeps_the_source_value():
    item = grade('G-7')
    assert item['grade_scale'] == 'UNKNOWN'
    assert item['grade_value'] == 4
    assert item['state'] == 'GRADED'
    assert any(warning.startswith('GRADE_SCALE_UNKNOWN') for warning in item['warnings'])


def test_missing_grade_id_has_no_fabricated_uid():
    item = grade('')
    assert item['grade_uid'] is None
    assert item['identity_state'] == 'MISSING_STABLE_ID'
    assert 'MISSING_STABLE_ID' in item['warnings']


def test_string_grade_value_is_rejected():
    invalid = fixture('invalid-rows.json')['grade_rows'][0]
    with pytest.raises(domain_models.RecordError) as caught:
        normalize_grade(invalid, semester_id=SEMESTER, observed_at=CHECKED,
                        source_evidence_id='academics.grades',
                        course_index=course_index(), source_id='academics.grades')
    assert caught.value.code == 'GRADE_VALUE_TYPE'


def test_negative_credits_are_rejected():
    invalid = fixture('invalid-rows.json')['grade_rows'][1]
    with pytest.raises(domain_models.RecordError) as caught:
        normalize_grade(invalid, semester_id=SEMESTER, observed_at=CHECKED,
                        source_evidence_id='academics.grades',
                        course_index=course_index(), source_id='academics.grades')
    assert caught.value.code == 'INVALID_CREDITS'


def test_graded_without_any_value_is_rejected():
    invalid = fixture('invalid-rows.json')['grade_rows'][2]
    with pytest.raises(domain_models.RecordError) as caught:
        normalize_grade(invalid, semester_id=SEMESTER, observed_at=CHECKED,
                        source_evidence_id='academics.grades',
                        course_index=course_index(), source_id='academics.grades')
    assert caught.value.code == 'GRADE_VALUE_MISSING'


def test_grade_state_and_value_must_agree():
    invalid = fixture('invalid-rows.json')['grade_rows'][3]
    with pytest.raises(domain_models.RecordError) as caught:
        normalize_grade(invalid, semester_id=SEMESTER, observed_at=CHECKED,
                        source_evidence_id='academics.grades',
                        course_index=course_index(), source_id='academics.grades')
    assert caught.value.code == 'INCONSISTENT_GRADE_STATE'


def test_grade_records_validate_against_grade_v1():
    for grade_id in ('G-1', 'G-2', 'G-3', 'G-4', 'G-5', 'G-6', 'G-9', ''):
        validate_contract('sources/data-models/grade.schema.json', grade(grade_id))


def test_grade_model_exposes_no_gpa_or_rank():
    schema = contract('sources/data-models/grade.schema.json')
    allowed = set(schema['properties'])
    for grade_id in ('G-1', 'G-2', 'G-5', 'G-6'):
        assert set(grade(grade_id)) <= allowed
        assert 'gpa' not in json.dumps(grade(grade_id)).lower()
        assert 'rank' not in json.dumps(grade(grade_id)).lower()


# ---------------------------------------------------------------- privacy

def test_grade_audit_log_contains_counts_only():
    records = [grade(grade_id) for grade_id in ('G-1', 'G-2', 'G-5', 'G-6')]
    audit = sensitivity.audit_record(semester_id=SEMESTER, records=records, checked_at=CHECKED)
    assert audit['row_count'] == 4
    assert audit['state_counts'] == {'GRADED': 2, 'UNGRADED': 1, 'WITHHELD': 1}
    assert audit['values_included'] is False
    assert sensitivity.contains_grade_values(audit) is False
    assert '92' not in json.dumps(audit, ensure_ascii=False)


def test_dashboard_summary_excludes_grade_values():
    records = [grade(grade_id) for grade_id in ('G-1', 'G-2', 'G-3')]
    summary = sensitivity.dashboard_summary(semester_id=SEMESTER, records=records)
    assert summary['values_included'] is False
    assert summary['reason'] == 'GRADES_EXCLUDED_FROM_PUBLIC_SUMMARY'
    assert sensitivity.contains_grade_values(summary) is False
    assert 'grade_value' not in summary and 'grade_text' not in summary


def test_model_context_requires_an_explicit_request():
    records = [grade('G-1')]
    default = sensitivity.model_context(records=records)
    assert default == {'included': False, 'reason': 'GRADES_REQUIRE_EXPLICIT_REQUEST', 'items': []}
    with pytest.raises(AcademicsError):
        sensitivity.model_context(records=records, explicit_request=True)
    included = sensitivity.model_context(records=records, explicit_request=True,
                                         purpose='USER_REQUESTED_GRADE_READ')
    assert included['included'] is True
    assert included['items'][0]['grade_value'] == 92


def test_plain_grade_cache_is_refused():
    assert sensitivity.require_encrypted_cache(True) is True
    with pytest.raises(AcademicsError) as caught:
        sensitivity.require_encrypted_cache(False)
    assert caught.value.code == 'ACADEMICS_GRADE_CACHE_FORBIDDEN'


def test_reader_grades_requires_an_explicit_request():
    reader = AcademicsReader()
    with pytest.raises(AcademicsError) as caught:
        reader.read('grades', semester=SEMESTER)
    assert caught.value.code == 'ACADEMICS_SENSITIVE_REQUEST_REQUIRED'
    with pytest.raises(AcademicsError):
        reader.read('grades', semester=SEMESTER, explicit_sensitive=True)
    blocked = reader.read('grades', semester=SEMESTER, explicit_sensitive=True,
                          purpose='USER_REQUESTED_GRADE_READ')
    assert blocked['items'] == [] and blocked['implementation_state'] == 'BLOCKED'
    assert 'audit' not in blocked


# ----------------------------------------------------------------- reader

@pytest.mark.parametrize('capability', ['timetable', 'exams', 'grades'])
def test_blocked_read_returns_the_frozen_blocked_shape(capability):
    reader = AcademicsReader()
    options = {'semester': SEMESTER}
    if capability == 'grades':
        options.update(explicit_sensitive=True, purpose='USER_REQUESTED_GRADE_READ')
    result = reader.read(capability, **options)
    validate_contract('result-envelope.schema.json', result)
    assert result['status'] == 'UNKNOWN'
    assert result['error']['code'] == 'ADAPTER_REQUIRED'
    assert result['error']['category'] == 'NOT_IMPLEMENTED'
    assert result['implementation_state'] == 'BLOCKED'
    assert result['runtime_availability'] == 'UNAVAILABLE'
    assert result['evidence_level'] == 'NONE'
    assert result['items'] == [] and result['data'] is None
    assert result['complete'] is False and result['source_complete'] is False
    assert 'SOURCE_CONTRACT_BLOCKED' in result['warnings']
    assert result['source']['service_id'] == 'academics.' + capability


def test_production_transport_is_refused_even_in_rehearsal():
    def live(capability, parameters):
        return {'synthetic': False, 'pages': []}

    with pytest.raises(AcademicsError) as caught:
        AcademicsReader().read_synthetic('timetable', live, semester=SEMESTER)
    assert caught.value.code == 'ACADEMICS_PRODUCTION_TRANSPORT_BLOCKED'
    live.synthetic = True
    with pytest.raises(AcademicsError):
        AcademicsReader().read_synthetic('timetable', live, semester=SEMESTER,
                                         purpose='USER_REQUESTED_ACADEMIC_REVIEW')


def test_rehearsal_keeps_blocked_state_and_synthetic_labels():
    result = rehearsal('timetable', timetable_rows())
    validate_contract('result-envelope.schema.json', result)
    validate_contract('source-evidence.schema.json', result['source_evidence'])
    assert result['status'] == 'OK'
    assert result['complete'] is True and result['source_complete'] is True
    assert result['implementation_state'] == 'BLOCKED'
    assert result['runtime_availability'] == 'UNAVAILABLE'
    assert result['evidence_level'] == 'SYNTHETIC_TRANSPORT'
    assert result['synthetic'] is True
    assert result['rehearsal'] == {'synthetic': True, 'purpose': 'SYNTHETIC_FIXTURE',
                                   'production_blocked': True}
    assert len(result['items']) == 9
    assert result['source_evidence']['synthetic'] is True
    assert all(item['synthetic'] is True for item in result['items'])


def test_second_page_failure_keeps_rows_and_reduces_completeness():
    transport = synthetic_transport([
        {'number': 1, 'rows': [row(exam_rows(), 'exam_id', 'EX-1')]},
        {'number': 2, 'failed': True, 'rows': []},
    ])
    result = AcademicsReader().read_synthetic('exams', transport, semester=SEMESTER)
    assert result['status'] == 'PARTIAL'
    assert result['complete'] is False
    assert result['source_complete'] is False
    assert result['missing_pages'] == [2]
    assert len(result['items']) == 1
    assert 'MISSING_SOURCE_PAGES' in result['partial_reasons']


def test_source_failure_is_never_an_empty_result():
    transport = synthetic_transport([], errors=['ACADEMICS_PAGE_FAILED'])
    result = AcademicsReader().read_synthetic('exams', transport, semester=SEMESTER)
    assert result['status'] != 'EMPTY'
    assert result['complete'] is False
    assert result['items'] == []
    assert 'ACADEMICS_PAGE_FAILED' in result['source_errors']


def test_semester_filter_and_ambiguous_titles_fail_closed():
    transport = synthetic_transport([{'number': 1, 'rows': timetable_rows()}])
    reader = AcademicsReader()
    with pytest.raises(AcademicsError) as caught:
        reader.read_synthetic('timetable', transport, semester='2027-2028-1')
    assert caught.value.code == 'ACADEMICS_INVALID_SEMESTER'
    with pytest.raises(AcademicsError):
        reader.read_synthetic('timetable', transport, semester='2026-2027 秋季学期')


def test_parameter_whitelist_rejects_url_and_cookie_fields():
    reader = AcademicsReader()
    with pytest.raises(AcademicsError) as caught:
        reader.read('timetable', semester=SEMESTER,
                    course={'course_id': 'X', 'url': 'https://example.invalid'})
    assert caught.value.code == 'ACADEMICS_INVALID_ARGUMENTS'
    with pytest.raises(AcademicsError):
        reader.read('timetable', semester=SEMESTER, course={'cookie': 'x'})
    with pytest.raises(AcademicsError):
        reader.read('timetable', semester='../etc/passwd')
    with pytest.raises(AcademicsError):
        reader.read('unknown-capability')


def test_no_public_registration_or_mcp_tool_was_added():
    services = {service for service, _operation in queries.REGISTRY}
    assert 'academics' not in services
    assert all(not str(service).startswith('academics') for service in services)
    assert academics.describe()['registration'].startswith('6.1')


def test_reader_can_be_built_without_a_transport():
    result = AcademicsReader().read('timetable', semester=SEMESTER)
    assert result['status'] == 'UNKNOWN' and result['items'] == []


# ----------------------------------------------------------------- events

def test_exam_event_validates_against_calendar_event_v1():
    for exam_id in ('EX-1', 'EX-2', 'EX-3', 'EX-9'):
        validate_contract('calendar-event.schema.json', academics_events.exam_event(exam(exam_id)))


def test_exam_event_status_is_derived_with_a_rule_version():
    event = academics_events.exam_event(exam('EX-9'))
    assert event['status'] == 'TENTATIVE'       # published release missing
    assert event['derivation']['rule_version'] == academics_events.EXAM_RULE
    assert event['derivation']['kind'] == 'DERIVED'
    assert event['source']['kind'] == 'EXAM'
    assert event['export']['state'] == 'NOT_EXPORTED'


def test_timetable_events_follow_expanded_occurrences():
    item = entry('TT-1')
    expanded = expansion('TT-1')
    converted = calendar_events('timetable', [item], expansions=[expanded])
    assert len(converted['events']) == 16
    assert converted['excluded'] == []
    first = converted['events'][0]
    validate_contract('calendar-event.schema.json', first)
    assert first['source']['kind'] == 'COURSE'
    assert first['status'] == 'TENTATIVE'
    assert first['event_uid'] == 'CAMPUS:UNKNOWN:COURSE:academics_timetable_TT-1:2026-09-14'
    assert len({event['event_uid'] for event in converted['events']}) == 16


def test_timetable_events_skip_an_unexpanded_entry():
    item = entry('TT-8')
    converted = calendar_events('timetable', [item], expansions={'TT-8': expansion('TT-8')})
    assert converted['events'] == []
    assert converted['excluded'][0]['reason'] == 'NOT_EXPANDED'


def test_grades_cannot_become_calendar_events():
    with pytest.raises(AcademicsError) as caught:
        calendar_events('grades', [grade('G-1')])
    assert caught.value.code == 'ACADEMICS_GRADE_NOT_EVENT'
    with pytest.raises(AcademicsError):
        calendar_events('unknown', [])


def test_grade_reader_result_never_copies_values_into_evidence_or_logs():
    result = rehearsal('grades', grade_rows())
    assert result['status'] in ('OK', 'PARTIAL')
    assert sensitivity.contains_grade_values(result['source_evidence']) is False
    assert sensitivity.contains_grade_values(result['audit']) is False
    assert result['audit']['values_included'] is False
    assert 'ROWS_OUTSIDE_SELECTED_SEMESTER' in result['warnings']
    assert len(result['items']) == 9
    assert all(item['semester_id'] == SEMESTER for item in result['items'])


# ------------------------------------------------------- record completeness

def test_timetable_entry_keeps_week_pattern_and_platform_time():
    item = entry('TT-1')
    pattern = item['week_pattern']
    assert pattern['weeks'] == list(range(1, 17))
    assert pattern['parity'] == 'ALL'
    assert pattern['weekday'] == 1
    assert pattern['periods'] == [1, 2]
    assert pattern['complete'] is True
    assert item['location'] == '合成教学楼 101'
    assert item['platform_times'] == {'start': '08:00', 'end': '09:35', 'source': 'PLATFORM'}
    ranged = normalize_timetable_entry(
        {'entry_id': 'TT-RANGE', 'semester': SEMESTER, 'course_id': 'SYNTH-COURSE-A',
         'weekday': 2, 'periods': [1, 2], 'start_week': 1, 'end_week': 2,
         'date_range': {'start': '2026-09-01', 'end': '2026-12-31'}},
        semester_id=SEMESTER, observed_at=CHECKED,
        source_evidence_id='academics.timetable', course_index=course_index())
    assert ranged['week_pattern']['date_range'] == {'start': '2026-09-01', 'end': '2026-12-31'}


def test_same_title_teaching_classes_have_distinct_entries():
    first = entry('TT-1')
    second = entry('TT-2')
    assert first['title'] == second['title'] == '合成数据结构'
    assert first['teaching_class_id'] != second['teaching_class_id']
    assert first['entry_uid'] != second['entry_uid']
    assert first['course_uid'] != second['course_uid']


def test_unindexed_course_id_keeps_source_and_semester_scope():
    item = entry('')
    assert item['entry_uid'] is None
    assert item['course_association'] == 'UNINDEXED_SOURCE_ID'
    assert item['course_uid'] == 'learn.courses:COURSE:2026-2027-1:SYNTH-UNKNOWN'
    assert 'COURSE_NOT_IN_SOURCE_INDEX' in item['warnings']


def test_every_record_carries_a_check_time_and_evidence_class():
    for item in (entry('TT-1'), exam('EX-1')):
        assert item['observed_at'] == CHECKED
        assert item['source_evidence_id'].startswith('academics.')
        assert item['synthetic'] is True
    # grade-v1 is frozen with additionalProperties=false, so the check time is
    # carried by the envelope; the record keeps its own source evidence id.
    item = grade('G-1')
    assert item['source_evidence_id'] == 'academics.grades'
    assert item['synthetic'] is True
    assert 'observed_at' not in item


def test_exam_release_and_seat_are_retained():
    published = exam('EX-2')
    unpublished = exam('EX-9')
    assert published['release_state'] == 'PUBLISHED'
    assert published['seat'] == 'SYNTH-SEAT-1'
    assert unpublished['release_state'] == 'UNPUBLISHED'


def test_empty_result_needs_a_complete_declared_source():
    complete = rehearsal('exams', [], coverage={'declared': True, 'complete': True})
    assert complete['status'] == 'EMPTY'
    assert complete['complete'] is True and complete['source_complete'] is True
    assert complete['items'] == []
    undeclared = rehearsal('exams', [], coverage={'declared': False, 'complete': False})
    assert undeclared['status'] != 'EMPTY'
    assert undeclared['complete'] is False


def test_rehearsal_rejects_a_transport_that_omits_synthetic_marker():
    def liar(capability, parameters):
        return {'synthetic': False, 'pages': [{'number': 1, 'rows': []}]}

    liar.synthetic = True
    with pytest.raises(AcademicsError):
        AcademicsReader().read_synthetic('exams', liar, semester=SEMESTER)


def test_base_academics_error_is_registered():
    error = AcademicsError().as_error()
    assert error['code'] == 'ACADEMICS_ERROR'
    assert error['category'] == 'INTERNAL'
    assert error['automatic_replay_allowed'] is False


def test_duplicate_source_page_is_not_double_counted():
    collected = academics.collect_pages([
        {'number': 1, 'rows': [{'exam_id': 'EX-1'}]},
        {'number': 1, 'rows': [{'exam_id': 'EX-2'}]},
    ])
    assert collected['pages_read'] == 1
    assert collected['source_complete'] is False
    assert 'DUPLICATE_SOURCE_PAGE' in collected['source_errors']
    assert len(collected['rows']) == 1


def test_invalid_rows_reduce_completeness_without_a_fabricated_value():
    bad = fixture('invalid-rows.json')['exam_rows'][0]
    transport = synthetic_transport([{'number': 1, 'rows': [row(exam_rows(), 'exam_id', 'EX-1'), bad]}])
    result = AcademicsReader().read_synthetic('exams', transport, semester=SEMESTER)
    assert result['status'] == 'PARTIAL'
    assert result['complete'] is False
    assert len(result['items']) == 1
    assert result['items'][0]['date'] == '2027-01-05'
    assert result['rejected'][0]['code'] == 'END_BEFORE_START'


def test_date_only_events_use_date_kind():
    item = entry('TT-1')
    date_only = expansion('TT-1', with_periods=False)
    converted = calendar_events('timetable', [item], expansions=[date_only])
    assert converted['events']
    assert all(event['time']['kind'] == 'DATE' for event in converted['events'])
    assert [event['time']['date'] for event in converted['events']] == \
        [occurrence['date'] for occurrence in date_only['occurrences']]
