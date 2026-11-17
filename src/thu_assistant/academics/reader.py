"""Read-only isolation boundary for personal timetable, exams and grades.

All three capabilities are ``BLOCKED_SOURCE_EVIDENCE`` in
``contracts/v0.2/sources/adapter-readiness.json``: no verified source or field
mapping exists.  The public ``AcademicsReader.read`` therefore returns an
explicit blocked result — status ``UNKNOWN`` with error ``ADAPTER_REQUIRED``,
``implementation_state=BLOCKED`` and an empty item list — and never a simulated
grade, exam or timetable.

The normalisation, week expansion and privacy rules are implemented and
exercised through ``read_synthetic``: a named engineering rehearsal that only
accepts a transport declaring ``synthetic=True`` and a registered synthetic
purpose.  That path labels every result ``SYNTHETIC_TRANSPORT`` and keeps
``implementation_state=BLOCKED``; it cannot be mistaken for a production read.

No transport is created here, no URL/header/cookie parameter is accepted and
no MCP/query registration is modified (6.1 owns that wiring).
"""
from __future__ import annotations

import re

from .. import domain_models, source_state
from . import events, models, rules, sensitivity
from .errors import AcademicsError

VERSION = 'academics-read-v1'
ENABLEMENT_VERSION = 'academics-enablement-v1'

_SEMESTER = re.compile(r'^[A-Za-z0-9_.-]{1,80}$')
_TEXT = re.compile(r'^[^\x00-\x1f]{0,240}$')

CAPABILITIES = {
    'timetable': {
        'source_id': 'academics.timetable',
        'operation': 'timetable',
        'sensitive': False,
        'start_conclusion': 'BLOCKED_SOURCE_EVIDENCE',
        'implementation_state': 'BLOCKED',
        'runtime_availability': 'UNAVAILABLE',
        'evidence_level': 'NONE',
        'blocked_scope': ['production timetable adapter'],
        'required_evidence': [
            'official personal timetable entry and access precondition',
            'term, course, week/period and room field inventory',
            'stable occurrence identity and completeness scope',
        ],
        'reason': 'No verified personal-timetable source or field mapping exists; the teaching calendar is not a substitute.',
    },
    'exams': {
        'source_id': 'academics.exams',
        'operation': 'exams',
        'sensitive': False,
        'start_conclusion': 'BLOCKED_SOURCE_EVIDENCE',
        'implementation_state': 'BLOCKED',
        'runtime_availability': 'UNAVAILABLE',
        'evidence_level': 'NONE',
        'blocked_scope': ['production exam adapter'],
        'required_evidence': [
            'official exam entry and access precondition',
            'exam identifier, course association and date/time precision',
            'release, cancellation and rescheduling state plus coverage proof',
        ],
        'reason': 'No verified exam source or result shape exists; the teaching calendar explicitly does not cover all exams.',
    },
    'grades': {
        'source_id': 'academics.grades',
        'operation': 'grades',
        'sensitive': True,
        'start_conclusion': 'BLOCKED_SOURCE_EVIDENCE',
        'implementation_state': 'BLOCKED',
        'runtime_availability': 'UNAVAILABLE',
        'evidence_level': 'NONE',
        'blocked_scope': ['production course-grade adapter'],
        'required_evidence': [
            'official course-grade entry and access precondition',
            'course/semester identifiers joinable to a stable course record',
            'grade scale, credits and publication/withheld semantics',
        ],
        'reason': 'No verified course-grade source or publication semantics exist; assignment grades are not course grades.',
    },
}

SCOPES = ('SEMESTER', 'COURSE', 'ALL')
ALLOWED_PARAMETERS = {'semester', 'course', 'scope', 'limit'}
COURSE_FIELDS = {'course_id', 'title', 'teaching_class_id'}


def _checked(value):
    return source_state.normalize_timestamp(value, source_state.now_iso())


def validate_parameters(capability, *, semester=None, course=None, scope='SEMESTER', limit=50):
    """Only registered business parameters are accepted.

    The schema deliberately has no field for a URL, request header, Cookie,
    upstream path or arbitrary JSON body: an academics read is not an internal
    network proxy.
    """
    if capability not in CAPABILITIES:
        raise AcademicsError('ACADEMICS_UNKNOWN_CAPABILITY', str(capability))
    if semester is not None and (not isinstance(semester, str) or not _SEMESTER.fullmatch(semester)):
        raise AcademicsError('ACADEMICS_INVALID_ARGUMENTS', 'semester')
    if course is not None:
        if not isinstance(course, dict) or set(course) - COURSE_FIELDS:
            raise AcademicsError('ACADEMICS_INVALID_ARGUMENTS', 'course')
        if not course:
            raise AcademicsError('ACADEMICS_INVALID_ARGUMENTS', 'course')
        for key, value in course.items():
            if not isinstance(value, str) or not _TEXT.fullmatch(value):
                raise AcademicsError('ACADEMICS_INVALID_ARGUMENTS', 'course.' + key)
    if scope not in SCOPES:
        raise AcademicsError('ACADEMICS_INVALID_ARGUMENTS', 'scope')
    if type(limit) is not int or not 1 <= limit <= 200:
        raise AcademicsError('ACADEMICS_INVALID_ARGUMENTS', 'limit')
    return {'semester': semester, 'course': course, 'scope': scope, 'limit': limit}


def operation_schemas():
    """Parameter schemas for the future public registration (6.1)."""
    text = {'type': 'string', 'maxLength': 240}
    course = {'type': 'object', 'properties': {
        key: {'type': 'string', 'maxLength': 240} for key in sorted(COURSE_FIELDS)},
        'additionalProperties': False}
    common = {'semester': {'type': 'string', 'pattern': r'^[A-Za-z0-9_.-]{1,80}$'},
              'course': course,
              'scope': {'type': 'string', 'enum': list(SCOPES)},
              'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}
    return {name: {'type': 'object', 'properties': dict(common), 'additionalProperties': False,
                   'category': 'read', 'sensitive': spec['sensitive'],
                   'implementation_state': spec['implementation_state']}
            for name, spec in CAPABILITIES.items()}


def capability_report():
    return {'version': ENABLEMENT_VERSION,
            'capabilities': {name: {key: value for key, value in spec.items()
                                    if key in ('source_id', 'operation', 'sensitive',
                                               'start_conclusion', 'implementation_state',
                                               'runtime_availability', 'evidence_level',
                                               'blocked_scope', 'required_evidence', 'reason')}
                             for name, spec in CAPABILITIES.items()}}


def describe():
    return {
        'service_id': 'academics',
        'version': VERSION,
        'enablement_version': ENABLEMENT_VERSION,
        'capabilities': capability_report()['capabilities'],
        'operations': operation_schemas(),
        'rules': {
            'week_expansion': rules.EXPANSION_RULE,
            'calendar': rules.CALENDAR_VERSION,
            'period_table': rules.PERIOD_TABLE_VERSION,
            'exam_event': events.EXAM_RULE,
            'timetable_event': events.TIMETABLE_RULE,
            'models': models.VERSION,
        },
        'grade_privacy': sensitivity.describe(),
        'registration': '6.1 registers public read operations; this module changes no queries.py registry and no MCP tool.',
        'live_reads_enabled': False,
    }


def blocked_result(capability, *, checked_at=None):
    """The explicit blocked result; never EMPTY and never a simulated record."""
    spec = CAPABILITIES[capability]
    checked = _checked(checked_at)
    result = {
        'status': 'UNKNOWN',
        'source': {'service_id': spec['source_id'], 'operation': spec['operation'],
                   'url': None, 'adapter_version': VERSION},
        'checked_at': checked,
        'verification_level': 'NONE',
        'source_complete': False,
        'complete': False,
        'snapshot_complete': False,
        'next_cursor': None,
        'warnings': ['SOURCE_CONTRACT_BLOCKED'],
        'missing_pages': [],
        'partial_reasons': ['SOURCE_CONTRACT_BLOCKED'],
        'source_state': 'UNKNOWN',
        'query_time': checked,
        'implementation_state': 'BLOCKED',
        'runtime_availability': 'UNAVAILABLE',
        'evidence_level': 'NONE',
        'capability': capability,
        'blocked_scope': list(spec['blocked_scope']),
        'items': [],
        'data': None,
        'total_items': 0,
        'error': source_state.public_error('ADAPTER_REQUIRED'),
    }
    return result


def collect_pages(pages, *, missing_pages=(), errors=()):
    """Collect source pages without turning a failed page into a complete read."""
    rows, read, missing, problems = [], [], set(), set()
    declared = 0
    for page in pages or ():
        if not isinstance(page, dict) or type(page.get('number')) is not int or page['number'] < 1:
            problems.add('ACADEMICS_INVALID_ARGUMENTS')
            continue
        declared += 1
        if page['number'] in read:
            problems.add('DUPLICATE_SOURCE_PAGE')
            continue
        if page.get('failed') is True or not isinstance(page.get('rows'), list):
            missing.add(page['number'])
            continue
        read.append(page['number'])
        rows.extend(row for row in page['rows'] if isinstance(row, dict))
        skipped = len([row for row in page['rows'] if not isinstance(row, dict)])
        if skipped:
            problems.add('ACADEMICS_INVALID_ARGUMENTS')
    for number in missing_pages or ():
        if type(number) is int and number >= 1:
            missing.add(number)
        else:
            problems.add('ACADEMICS_INVALID_ARGUMENTS')
    for error in errors or ():
        code = error.get('code') if isinstance(error, dict) else error
        if isinstance(code, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{2,80}', code):
            problems.add(code)
        elif code not in (None, ''):
            problems.add('SOURCE_ERROR')
    expected = max([declared, *sorted(missing), *read]) if declared or missing else None
    source_complete = bool(declared) and not missing and not problems
    return {
        'rows': rows,
        'pages_read': len(read),
        'expected_pages': expected,
        'missing_pages': sorted(missing),
        'source_errors': sorted(problems),
        'source_complete': source_complete,
        'upstream_total': None,
    }


def _call_transport(transport, capability, parameters):
    if hasattr(transport, 'read'):
        return transport.read(capability, parameters)
    return transport(capability, parameters)


def _declared_synthetic(transport):
    return getattr(transport, 'synthetic', False) is True


class AcademicsReader:
    """Production entry point; every capability currently returns blocked."""

    def __init__(self, *, account_scope='CAMPUS', clock=None):
        self.account_scope = account_scope
        self.clock = clock or source_state.now_iso

    def now(self):
        return _checked(self.clock())

    def read(self, capability, *, semester=None, course=None, scope='SEMESTER',
             limit=50, explicit_sensitive=False, purpose=None):
        validate_parameters(capability, semester=semester, course=course, scope=scope, limit=limit)
        spec = CAPABILITIES[capability]
        if spec['sensitive'] and not explicit_sensitive:
            raise AcademicsError('ACADEMICS_SENSITIVE_REQUEST_REQUIRED', 'grades')
        if spec['sensitive'] and purpose not in sensitivity.PURPOSES - {'SYNTHETIC_FIXTURE'}:
            raise AcademicsError('ACADEMICS_SENSITIVE_REQUEST_REQUIRED', 'purpose')
        # No source contract is verified, so there is deliberately no path from
        # this method to a transport.  Production reads stay blocked.
        return blocked_result(capability, checked_at=self.now())

    def read_synthetic(self, capability, transport, *, semester=None, course=None,
                       scope='SEMESTER', limit=50, explicit_sensitive=False,
                       purpose='SYNTHETIC_FIXTURE'):
        """Engineering rehearsal with a transport that declares itself synthetic."""
        if not _declared_synthetic(transport):
            raise AcademicsError('ACADEMICS_PRODUCTION_TRANSPORT_BLOCKED', 'synthetic')
        if purpose != 'SYNTHETIC_FIXTURE':
            raise AcademicsError('ACADEMICS_SENSITIVE_REQUEST_REQUIRED', 'purpose')
        parameters = validate_parameters(capability, semester=semester, course=course,
                                         scope=scope, limit=limit)
        if CAPABILITIES[capability]['sensitive'] and not explicit_sensitive:
            raise AcademicsError('ACADEMICS_SENSITIVE_REQUEST_REQUIRED', 'grades')
        raw = _call_transport(transport, capability, parameters)
        if not isinstance(raw, dict) or raw.get('synthetic') is not True:
            raise AcademicsError('ACADEMICS_PRODUCTION_TRANSPORT_BLOCKED', 'synthetic')
        return self._finish(capability, raw, parameters)

    # -- normalisation -------------------------------------------------
    def _finish(self, capability, raw, parameters):
        spec = CAPABILITIES[capability]
        checked = _checked(raw.get('checked_at') or self.now())
        collected = collect_pages(raw.get('pages') or (), missing_pages=raw.get('missing_pages') or (),
                                  errors=raw.get('errors') or ())
        coverage = raw.get('coverage') if isinstance(raw.get('coverage'), dict) else {}
        coverage_declared = coverage.get('declared') is True
        coverage_complete = coverage.get('complete') is True
        warnings = set(collected['source_errors'])
        warnings |= {'SOURCE_COVERAGE_UNDECLARED'} if not coverage_declared else set()
        warnings |= {'SOURCE_COVERAGE_INCOMPLETE'} if coverage_declared and not coverage_complete else set()
        if not coverage_declared and capability == 'exams':
            warnings.add('EXAM_COVERAGE_NOT_DECLARED')

        semester_selection = None
        semester_id = parameters['semester']
        if capability in ('timetable', 'exams', 'grades'):
            semester_rows = []
            for row in raw.get('semesters') or ():
                if not isinstance(row, dict):
                    continue
                try:
                    semester_rows.append(models.normalize_semester(row, synthetic=True))
                except domain_models.RecordError:
                    continue
            semester_selection = models.select_semester(semester_rows, semester_id)
            if semester_selection['state'] != 'SELECTED':
                raise AcademicsError('ACADEMICS_INVALID_SEMESTER', semester_selection['state'])
            semester_id = semester_selection['semester']['semester_id']
            warnings |= set(semester_selection['warnings'])
            kept, foreign = [], 0
            for row in collected['rows']:
                declared = row.get('semester') or row.get('semester_id')
                if declared not in (None, '') and str(declared) != semester_id:
                    foreign += 1
                    continue
                kept.append(row)
            if foreign:
                warnings.add('ROWS_OUTSIDE_SELECTED_SEMESTER')
            collected['rows'] = kept
        index = models.CourseIndex(
            raw.get('courses') or (), source_id=raw.get('course_source_id') or 'learn.courses',
            observed_at=checked, source_evidence_id=raw.get('course_source_id') or 'learn.courses',
            account_scope=self.account_scope, synthetic=True)

        records, rejected, record_warnings = [], [], set()
        if capability == 'timetable':
            outcome = models.normalize_rows(
                collected['rows'], models.normalize_timetable_entry,
                semester_id=semester_id, observed_at=checked,
                source_evidence_id=spec['source_id'], account_scope=self.account_scope,
                course_index=index, source_id=spec['source_id'], synthetic=True)
            merged = models.merge_records(outcome['records'])
            records = merged['records']
            rejected = outcome['rejected']
            record_warnings |= set(outcome['warnings']) | set(merged['warnings'])
        elif capability == 'exams':
            outcome = models.normalize_rows(
                collected['rows'], models.normalize_exam,
                semester_id=semester_id, observed_at=checked,
                source_evidence_id=spec['source_id'], account_scope=self.account_scope,
                course_index=index, source_id=spec['source_id'], synthetic=True)
            merged = models.merge_records(outcome['records'])
            records = merged['records']
            rejected = outcome['rejected']
            record_warnings |= set(outcome['warnings']) | set(merged['warnings'])
        else:
            outcome = models.normalize_rows(
                collected['rows'], models.normalize_grade,
                semester_id=semester_id, observed_at=checked,
                source_evidence_id=spec['source_id'], account_scope=self.account_scope,
                course_index=index, source_id=spec['source_id'], synthetic=True)
            merged = domain_models.deduplicate(outcome['records'])
            records = merged['records']
            rejected = outcome['rejected']
            record_warnings |= set(outcome['warnings']) | set(merged['warnings'])
        warnings |= record_warnings

        source_complete = collected['source_complete'] and coverage_declared and coverage_complete
        failure = bool(collected['missing_pages'] or collected['source_errors'] or rejected)
        if failure:
            status = 'PARTIAL'
        elif not coverage_declared or not coverage_complete:
            status = 'PARTIAL' if capability == 'exams' else ('OK' if records else 'UNKNOWN')
        elif records:
            status = 'OK'
        else:
            # An empty list is EMPTY only when the source was actually read and
            # declared complete; otherwise the outcome is unknown.
            status = 'EMPTY' if source_complete else 'UNKNOWN'
        flags = source_state.completeness(status, source_complete, True,
                                          collected['missing_pages'],
                                          collected['source_errors'], warnings)
        # An object list that exists but cannot be recorded as a complete
        # business result must not claim completeness.
        if status == 'OK' and not flags['complete']:
            status = 'PARTIAL'
            flags = source_state.completeness(status, source_complete, True,
                                              collected['missing_pages'],
                                              collected['source_errors'], warnings)
        scope = source_state.read_scope(
            'FULL_LIST' if parameters['scope'] in ('SEMESTER', 'ALL') else 'WINDOW',
            offset=0, limit=max(1, len(records)), pages_read=collected['pages_read'],
            expected_pages=collected['expected_pages'], query_time=checked, window=None)
        evidence = source_state.source_evidence(
            source_id=spec['source_id'], service=spec['source_id'],
            operation=spec['operation'], adapter_version=raw.get('adapter_version') or VERSION,
            checked_at=checked, state=status, scope=scope, row_count=len(records),
            upstream_total=raw.get('upstream_total'),
            deduplicated_entries=max(0, len(collected['rows']) - len(records)),
            source_complete=source_complete, complete=flags['complete'],
            missing_pages=collected['missing_pages'], warnings=sorted(flags['warnings']),
            verification_level='NONE', synthetic=True, url=raw.get('url'),
            http_status=raw.get('http_status'), auth_state='UNKNOWN',
            rows=None if capability == 'grades' else records)
        result = {
            'status': status,
            'source': {'service_id': spec['source_id'], 'operation': spec['operation'],
                       'url': source_state.sanitize_url(raw.get('url')), 'adapter_version': VERSION},
            'checked_at': checked,
            'verification_level': 'NONE',
            'source_complete': flags['source_complete'],
            'snapshot_complete': True,
            'complete': flags['complete'],
            'next_cursor': None,
            'warnings': flags['warnings'],
            'missing_pages': flags['missing_pages'],
            'partial_reasons': flags['partial_reasons'],
            'source_state': source_state.evidence_state(status),
            'query_time': checked,
            'read_scope': scope,
            'source_evidence': evidence,
            'capability': capability,
            'implementation_state': 'BLOCKED',
            'runtime_availability': 'UNAVAILABLE',
            'evidence_level': 'SYNTHETIC_TRANSPORT',
            'synthetic': True,
            'rehearsal': {'synthetic': True, 'purpose': 'SYNTHETIC_FIXTURE',
                          'production_blocked': True},
            'items': records,
            'total_items': len(records),
            'rejected': rejected,
            'semester_selection': (None if semester_selection is None else
                                   {'state': semester_selection['state'],
                                    'basis': semester_selection['basis'],
                                    'semester_id': (semester_selection['semester'] or {}).get('semester_id'),
                                    'candidates': semester_selection['candidates']}),
        }
        if flags['source_errors']:
            result['source_errors'] = flags['source_errors']
        if status in source_state.ERROR_OBJECT_STATUSES:
            result['error'] = source_state.public_error(status)
        if capability == 'grades':
            result['audit'] = sensitivity.audit_record(
                semester_id=semester_id, records=records, checked_at=checked,
                source_id=spec['source_id'], explicit_request=True)
        return result


def read_synthetic(capability, transport, **options):
    """Convenience wrapper around ``AcademicsReader.read_synthetic``."""
    return AcademicsReader().read_synthetic(capability, transport, **options)


__all__ = [
    'ALLOWED_PARAMETERS', 'AcademicsReader', 'CAPABILITIES', 'COURSE_FIELDS',
    'ENABLEMENT_VERSION', 'SCOPES', 'VERSION', 'blocked_result',
    'capability_report', 'collect_pages', 'describe', 'operation_schemas',
    'read_synthetic', 'validate_parameters',
]
