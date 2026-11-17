"""Normalisation of semesters, courses, timetable entries, exams and grades.

The frozen v0.2 models (``course-v1``, ``grade-v1``) and the shared identity /
time rules in :mod:`thu_assistant.domain_models` are reused rather than
re-declared.  Timetable entries and exam records are module-local shapes: no
frozen schema exists for them, so every field carries an explicit meaning and
the stable identifier rule is documented here instead of pretending the model
is already registered.

Key rules:

* a semester or course is never selected by a near title; only an exact source
  identifier (or an exact unique title with an explicit warning) selects one;
* two records with the same title but different source identifiers stay two
  records, and a title-only association never produces a ``course_uid``;
* exam dates keep their precision: a date-only exam has no clock time and no
  synthesised midnight;
* a missing grade is ``null`` with ``UNGRADED``/``WITHHELD``/``UNKNOWN`` and is
  never zero; a zero grade is ``GRADED`` with value ``0``;
* unfamiliar statuses stay ``UNKNOWN`` with a non-reversible digest.
"""
from __future__ import annotations

from datetime import date, datetime
import html
import re

from .. import domain_models
from ..domain_models import RecordError
from .errors import AcademicsError
from . import rules

VERSION = 'academics-models-v1'

SOURCE_ID = re.compile(r'^[a-z][a-z0-9_.-]{2,120}$')
PART = re.compile(r'^[A-Za-z0-9_.:+-]{1,240}$')
UID = re.compile(r'^[a-z][a-z0-9_.-]{1,80}:[A-Z]+:[A-Za-z0-9_.:+-]{1,180}$')
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
CLOCK = re.compile(r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')

EXAM_STATE_MAP = {
    'scheduled': 'SCHEDULED', 'normal': 'SCHEDULED', 'confirmed': 'SCHEDULED',
    '安排': 'SCHEDULED', '正常': 'SCHEDULED',
    'rescheduled': 'RESCHEDULED', 'changed': 'RESCHEDULED', 'moved': 'RESCHEDULED',
    '改期': 'RESCHEDULED', '调整': 'RESCHEDULED',
    'makeup': 'MAKEUP', 'retake': 'MAKEUP', '补考': 'MAKEUP',
    'deferred': 'DEFERRED', 'postponed': 'DEFERRED', '缓考': 'DEFERRED', '延期': 'DEFERRED',
    'cancelled': 'CANCELLED', 'canceled': 'CANCELLED', '取消': 'CANCELLED',
}
RELEASE_STATE_MAP = {
    'published': 'PUBLISHED', 'released': 'PUBLISHED', '已发布': 'PUBLISHED', '公布': 'PUBLISHED',
    'unpublished': 'UNPUBLISHED', 'not_published': 'UNPUBLISHED', '未发布': 'UNPUBLISHED',
    'withheld': 'WITHHELD', 'hidden': 'WITHHELD', '扣留': 'WITHHELD', '暂不公布': 'WITHHELD',
}
GRADE_STATE_MAP = {
    'graded': 'GRADED', 'published': 'GRADED', 'released': 'GRADED',
    '已发布': 'GRADED', '已出分': 'GRADED',
    'ungraded': 'UNGRADED', 'unpublished': 'UNGRADED', 'not_published': 'UNGRADED',
    '未发布': 'UNGRADED', '未出分': 'UNGRADED',
    'withheld': 'WITHHELD', 'hidden': 'WITHHELD', '扣留': 'WITHHELD',
    'unknown': 'UNKNOWN',
}
GRADE_SCALE_MAP = {
    'numeric': 'NUMERIC_100', 'numeric_100': 'NUMERIC_100', '100': 'NUMERIC_100',
    '百分制': 'NUMERIC_100', '分数': 'NUMERIC_100',
    'level': 'GRADE_LEVEL', 'grade_level': 'GRADE_LEVEL', '等级制': 'GRADE_LEVEL', '等级': 'GRADE_LEVEL',
    'pass_fail': 'PASS_FAIL', 'pass/fail': 'PASS_FAIL', 'pf': 'PASS_FAIL',
    '通过制': 'PASS_FAIL', '二级制': 'PASS_FAIL',
}


def _text(value):
    if value is None:
        return ''
    return CONTROL.sub('', html.unescape(str(value)).strip())


def _first(*values):
    for value in values:
        text = _text(value)
        if text:
            return text
    return ''


def _part(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not text or len(text) > 240 or not PART.fullmatch(text):
        return None
    return text


def _stable_uid(source_id, kind, *parts):
    """Module-local stable identifier; missing components yield ``None``."""
    if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
        return {'uid': None, 'identity_state': 'MISSING_STABLE_ID'}
    values = [_part(part) for part in parts]
    if not values or any(value is None for value in values):
        return {'uid': None, 'identity_state': 'MISSING_STABLE_ID'}
    uid = ':'.join([source_id, kind, *values])
    if len(uid) > 280 or not UID.fullmatch(uid):
        return {'uid': None, 'identity_state': 'MISSING_STABLE_ID'}
    return {'uid': uid, 'identity_state': 'STABLE'}


def _date_only(value):
    if value is None or value == '':
        return None, None
    parsed = domain_models.canonical_time(value)
    if not parsed['valid']:
        return None, 'DATE_INVALID'
    if parsed['date_precision'] != 'date':
        return None, 'DATE_PRECISION_NOT_DAY'
    return parsed['value'], None


def _observed(value):
    parsed = domain_models.canonical_time(value)
    if not parsed['valid'] or parsed['date_precision'] == 'date':
        raise RecordError('INVALID_OBSERVED_AT', 'observed_at')
    return parsed['value']


def normalize_semester(row, *, source_id='learn.semesters', synthetic=True):
    """Normalise one semester row; a missing id is kept without an identity."""
    if not isinstance(row, dict):
        raise RecordError('INVALID_RECORD', 'row')
    title = domain_models.require_title(_first(row.get('title'), row.get('name'), row.get('semester_name')))
    semester_id = _first(row.get('semester_id'), row.get('id'), row.get('term'))[:80]
    warnings = []
    starts_on, start_issue = _date_only(row.get('starts_on', row.get('start_date')))
    ends_on, end_issue = _date_only(row.get('ends_on', row.get('end_date')))
    for issue in (start_issue, end_issue):
        if issue:
            warnings.append(issue)
    identity_state = 'STABLE' if semester_id else 'MISSING_STABLE_ID'
    if not semester_id:
        warnings.append('SEMESTER_ID_MISSING')
    if starts_on and ends_on and starts_on > ends_on:
        warnings.append('SEMESTER_RANGE_INVERTED')
        starts_on = ends_on = None
    current = row.get('current')
    return {
        'source_id': source_id,
        'semester_id': semester_id or None,
        'title': title,
        'starts_on': starts_on,
        'ends_on': ends_on,
        'current': current if isinstance(current, bool) else None,
        'identity_state': identity_state,
        'warnings': sorted(set(warnings)),
        'synthetic': bool(synthetic),
    }


def select_semester(records, requested=None, *, allow_title_fallback=False):
    """Select one semester without merging equal titles.

    Returns ``state`` (``SELECTED``/``MISSING``/``AMBIGUOUS``), the selected
    record, the candidate ids and the selection basis.  A title is only used
    when it matches exactly once and the caller explicitly allows it; a title
    never becomes an identity.
    """
    rows = [row for row in records or () if isinstance(row, dict)]
    warnings = []
    by_id = {}
    conflicts = set()
    for row in rows:
        semester_id = row.get('semester_id')
        if not semester_id:
            continue
        if semester_id in by_id and by_id[semester_id].get('title') != row.get('title'):
            conflicts.add(semester_id)
            continue
        by_id[semester_id] = row
    if conflicts:
        warnings.append('SEMESTER_SOURCE_CONFLICT')
    candidates = sorted(by_id)
    if requested not in (None, ''):
        text = _text(requested)
        if text in by_id and text not in conflicts:
            return {'state': 'SELECTED', 'basis': 'SOURCE_ID', 'semester': by_id[text],
                    'candidates': candidates, 'warnings': warnings}
        matches = [row for row in by_id.values() if row.get('title') == text]
        if not matches:
            return {'state': 'MISSING', 'basis': None, 'semester': None,
                    'candidates': candidates, 'warnings': sorted(set(warnings) | {'SEMESTER_NOT_FOUND'})}
        if len(matches) > 1 or not allow_title_fallback:
            return {'state': 'AMBIGUOUS', 'basis': None, 'semester': None,
                    'candidates': [row['semester_id'] for row in matches],
                    'warnings': sorted(set(warnings) | {'SEMESTER_TITLE_NOT_IDENTITY'})}
        return {'state': 'SELECTED', 'basis': 'TITLE_EXACT', 'semester': matches[0],
                'candidates': candidates, 'warnings': sorted(set(warnings) | {'SEMESTER_SELECTED_BY_TITLE'})}
    current = [row for row in by_id.values() if row.get('current') is True]
    if len(current) == 1:
        return {'state': 'SELECTED', 'basis': 'CURRENT', 'semester': current[0],
                'candidates': candidates, 'warnings': warnings}
    if len(current) > 1:
        return {'state': 'AMBIGUOUS', 'basis': None, 'semester': None,
                'candidates': [row['semester_id'] for row in current],
                'warnings': sorted(set(warnings) | {'MULTIPLE_CURRENT_SEMESTERS'})}
    return {'state': 'MISSING', 'basis': None, 'semester': None,
            'candidates': candidates, 'warnings': sorted(set(warnings) | {'CURRENT_SEMESTER_UNKNOWN'})}


class CourseIndex:
    """A course catalogue keyed by source id, teaching class and exact title."""

    def __init__(self, rows, *, source_id='learn.courses', observed_at,
                 source_evidence_id, account_scope='CAMPUS', synthetic=True):
        if not SOURCE_ID.fullmatch(source_id or ''):
            raise AcademicsError('ACADEMICS_INVALID_ARGUMENTS', 'source_id')
        self.source_id = source_id
        self.account_scope = account_scope
        self.records = []
        self.rejected = []
        self._by_id = {}
        self._by_class = {}
        self._by_title = {}
        for row in rows or ():
            if not isinstance(row, dict):
                self.rejected.append({'code': 'INVALID_RECORD', 'field': 'row'})
                continue
            semester = _first(row.get('semester'), row.get('semester_id'))
            try:
                record = domain_models.normalize_course(
                    row, source_id=source_id, semester_id=semester,
                    observed_at=observed_at, source_evidence_id=source_evidence_id,
                    synthetic=synthetic)
            except RecordError as error:
                self.rejected.append({'code': error.code, 'field': error.field})
                continue
            self.records.append(record)
            if record.get('course_uid'):
                self._by_id[(record['semester_id'], record['source_record_id'])] = record
            class_id = _part(row.get('teaching_class_id'))
            if class_id and record.get('course_uid'):
                self._by_class[(record['semester_id'], class_id)] = record
            title = _text(record.get('title')).casefold()
            if title:
                self._by_title.setdefault((record['semester_id'], title), []).append(record)
        self.deduped = domain_models.deduplicate(self.records)

    def associate(self, *, course_id=None, title=None, class_id=None, semester_id=None):
        """Associate a record with a course without inventing an identity."""
        warnings = []
        semester = _text(semester_id) or None
        class_key = _part(class_id)
        record_key = _part(course_id)
        if semester is None:
            return {'association_state': 'MISSING_SEMESTER', 'course_uid': None,
                    'candidates': [], 'title': None,
                    'warnings': ['COURSE_ASSOCIATION_UNVERIFIED']}
        if class_key and (semester, class_key) in self._by_class:
            record = self._by_class[(semester, class_key)]
            if record_key and record.get('source_record_id') != record_key:
                warnings.append('CLASS_AND_COURSE_ID_DISAGREE')
            return {'association_state': 'EXACT_CLASS_ID', 'course_uid': record['course_uid'],
                    'candidates': [record['course_uid']], 'title': record['title'],
                    'warnings': warnings}
        if record_key:
            record = self._by_id.get((semester, record_key))
            if record is not None:
                if title and _text(title) != record.get('title'):
                    warnings.append('COURSE_TITLE_MISMATCH')
                return {'association_state': 'EXACT_ID', 'course_uid': record['course_uid'],
                        'candidates': [record['course_uid']], 'title': record['title'],
                        'warnings': warnings}
            ident = domain_models.course_uid(self.source_id, semester, record_key)
            return {'association_state': 'UNINDEXED_SOURCE_ID', 'course_uid': ident['uid'],
                    'candidates': [], 'title': None,
                    'warnings': sorted(set(warnings) | {'COURSE_NOT_IN_SOURCE_INDEX'})}
        text = _text(title).casefold()
        matches = self._by_title.get((semester, text), []) if text else []
        candidates = [record['course_uid'] for record in matches if record.get('course_uid')]
        if len(matches) == 1:
            return {'association_state': 'TITLE_ONLY_UNIQUE', 'course_uid': None,
                    'candidates': candidates, 'title': matches[0]['title'],
                    'warnings': ['COURSE_ASSOCIATION_UNVERIFIED']}
        if len(matches) > 1:
            return {'association_state': 'AMBIGUOUS_TITLE', 'course_uid': None,
                    'candidates': candidates, 'title': None,
                    'warnings': ['SAME_TITLE_DIFFERENT_IDENTITY']}
        return {'association_state': 'NOT_FOUND', 'course_uid': None,
                'candidates': [], 'title': None,
                'warnings': ['COURSE_ASSOCIATION_UNVERIFIED']}

    def describe(self):
        return {'version': VERSION, 'source_id': self.source_id,
                'courses': len(self.deduped['records']),
                'rejected': len(self.rejected),
                'relations': self.deduped['relations'],
                'warnings': self.deduped['warnings']}


def _association(index, row, semester_id, title):
    if index is None:
        return {'association_state': 'NO_COURSE_INDEX', 'course_uid': None,
                'candidates': [], 'title': None,
                'warnings': ['COURSE_ASSOCIATION_UNVERIFIED']}
    return index.associate(course_id=_first(row.get('course_id'), row.get('id_course')),
                           title=title, class_id=_first(row.get('teaching_class_id'), row.get('class_id')),
                           semester_id=semester_id)


def _platform_times(row):
    raw = row.get('platform_times')
    if not isinstance(raw, dict):
        return None, []
    start = _text(raw.get('start'))
    end = _text(raw.get('end'))
    if not start or not end:
        return None, ['PLATFORM_TIMES_INCOMPLETE']
    if not CLOCK.fullmatch(start) or not CLOCK.fullmatch(end) or end <= start:
        return None, ['PLATFORM_TIMES_INVALID']
    return {'start': start, 'end': end, 'source': 'PLATFORM'}, []


def normalize_timetable_entry(row, *, semester_id, observed_at, source_evidence_id,
                              account_scope='CAMPUS', course_index=None,
                              source_id='academics.timetable', synthetic=True):
    """Normalise one weekly timetable entry without expanding it."""
    if not isinstance(row, dict):
        raise RecordError('INVALID_RECORD', 'row')
    semester = _text(semester_id) or _first(row.get('semester'), row.get('semester_id'))[:80]
    record_id = _first(row.get('entry_id'), row.get('record_id'), row.get('id'))[:240]
    ident = _stable_uid(source_id, 'TIMETABLE', semester, record_id)
    title_hint = _first(row.get('title'), row.get('course_name'))
    association = _association(course_index, row, semester, title_hint)
    title = domain_models.require_title(_first(title_hint, association.get('title')))
    pattern = rules.normalize_week_pattern(row)
    observed = _observed(observed_at)
    evidence = _text(source_evidence_id)[:120]
    if not evidence:
        raise RecordError('MISSING_SOURCE_EVIDENCE_ID', 'source_evidence_id')
    platform_times, platform_warnings = _platform_times(row)
    warnings = set(pattern.get('warnings') or []) | set(association['warnings']) | set(platform_warnings)
    if ident['identity_state'] == 'MISSING_STABLE_ID':
        warnings.add('MISSING_STABLE_ID')
    teachers = row.get('teacher_names') or row.get('teachers')
    if isinstance(teachers, str):
        teachers = [teachers]
    if not isinstance(teachers, list):
        teachers = []
    return {
        'kind': 'TIMETABLE_ENTRY',
        'source_id': source_id,
        'source_record_id': record_id or None,
        'entry_uid': ident['uid'],
        'identity_state': ident['identity_state'],
        'dedupe_state': 'NOT_DEDUPED',
        'semester_id': semester or None,
        'account_scope': account_scope,
        'course_uid': association['course_uid'],
        'course_association': association['association_state'],
        'course_candidates': association['candidates'],
        'title': title,
        'teacher_names': [_text(value)[:200] for value in teachers if _text(value)][:40],
        'teaching_class_id': _part(row.get('teaching_class_id') or row.get('class_id')),
        'attempt_id': _part(row.get('attempt_id') or row.get('attempt')),
        'week_pattern': pattern,
        'location': _first(row.get('location'), row.get('room'), row.get('dd'))[:300] or None,
        'platform_times': platform_times,
        'source_status': _first(row.get('source_status'), row.get('status')) or None,
        'observed_at': observed,
        'source_evidence_id': evidence,
        'warnings': sorted(warnings),
        'synthetic': bool(synthetic),
    }


def normalize_exam(row, *, semester_id, observed_at, source_evidence_id,
                   account_scope='CAMPUS', course_index=None,
                   source_id='academics.exams', synthetic=True):
    """Normalise one exam record; date-only stays date-only."""
    if not isinstance(row, dict):
        raise RecordError('INVALID_RECORD', 'row')
    semester = _text(semester_id) or _first(row.get('semester'), row.get('semester_id'))[:80]
    record_id = _first(row.get('exam_id'), row.get('record_id'), row.get('id'))[:240]
    ident = _stable_uid(source_id, 'EXAM', semester, record_id)
    title_hint = _first(row.get('title'), row.get('course_name'), row.get('exam_name'))
    association = _association(course_index, row, semester, title_hint)
    title = domain_models.require_title(_first(title_hint, association.get('title')))
    warnings = set(association['warnings'])
    if ident['identity_state'] == 'MISSING_STABLE_ID':
        warnings.add('MISSING_STABLE_ID')
    evidence = _text(source_evidence_id)[:120]

    raw_date = row.get('date', row.get('exam_date'))
    exam_date = None
    start_time = _text(row.get('start_time'))
    end_time = _text(row.get('end_time'))
    precision = 'unknown'
    if raw_date not in (None, ''):
        parsed = domain_models.canonical_time(raw_date)
        if not parsed['valid']:
            warnings.add('EXAM_DATE_INVALID')
        elif parsed['date_precision'] == 'date':
            exam_date, precision = parsed['value'], 'date'
        else:
            exam_date, precision = parsed['value'][:10], 'minute'
            start_time = start_time or parsed['value'][11:16]
    if start_time and exam_date is None:
        raise RecordError('EXAM_TIME_WITHOUT_DATE', 'date')
    if start_time and not CLOCK.fullmatch(start_time):
        raise RecordError('INVALID_EXAM_TIME', 'start_time')
    if end_time and not CLOCK.fullmatch(end_time):
        raise RecordError('INVALID_EXAM_TIME', 'end_time')
    if start_time and end_time and end_time <= start_time:
        raise RecordError('END_BEFORE_START', 'end_time')
    if start_time:
        precision = 'minute'
    elif exam_date:
        precision = 'date'
        if end_time:
            raise RecordError('END_WITHOUT_START', 'end_time')

    if not evidence:
        raise RecordError('MISSING_SOURCE_EVIDENCE_ID', 'source_evidence_id')
    state = domain_models.mapped_status(row.get('state', row.get('status')), EXAM_STATE_MAP)
    release = domain_models.mapped_status(
        row.get('release_state', row.get('release')), RELEASE_STATE_MAP)
    warnings = set(association['warnings'])
    if ident['identity_state'] == 'MISSING_STABLE_ID':
        warnings.add('MISSING_STABLE_ID')
    if state['source_status_digest']:
        warnings.add('EXAM_STATE_UNKNOWN:' + state['source_status_digest'])
    if release['source_status_digest']:
        warnings.add('EXAM_RELEASE_UNKNOWN:' + release['source_status_digest'])
    return {
        'kind': 'EXAM',
        'source_id': source_id,
        'source_record_id': record_id or None,
        'exam_uid': ident['uid'],
        'identity_state': ident['identity_state'],
        'dedupe_state': 'NOT_DEDUPED',
        'semester_id': semester or None,
        'account_scope': account_scope,
        'course_uid': association['course_uid'],
        'course_association': association['association_state'],
        'course_candidates': association['candidates'],
        'title': title,
        'date': exam_date,
        'start_time': start_time or None,
        'end_time': end_time or None,
        'time_precision': precision,
        'state': state['status'],
        'state_mapping': state['mapping_state'],
        'release_state': release['status'],
        'release_mapping': release['mapping_state'],
        'location': _first(row.get('location'), row.get('room'))[:300] or None,
        'seat': _first(row.get('seat'), row.get('seat_no'))[:80] or None,
        'source_status': _first(row.get('source_status'), row.get('status')) or None,
        'observed_at': _observed(observed_at),
        'source_evidence_id': evidence,
        'warnings': sorted(warnings),
        'synthetic': bool(synthetic),
    }


def normalize_grade(row, *, semester_id, observed_at, source_evidence_id,
                    account_scope='CAMPUS', course_index=None,
                    source_id='academics.grades', synthetic=True):
    """Normalise one course grade to ``grade-v1`` without inventing values."""
    if not isinstance(row, dict):
        raise RecordError('INVALID_RECORD', 'row')
    semester = _text(semester_id) or _first(row.get('semester'), row.get('semester_id'))[:80]
    record_id = _first(row.get('grade_id'), row.get('record_id'), row.get('id'))[:240]
    ident = domain_models.grade_uid(source_id, semester, record_id)
    title_hint = _first(row.get('title'), row.get('course_name'))
    association = _association(course_index, row, semester, title_hint)
    warnings = set(association['warnings'])
    evidence = _text(source_evidence_id)[:120]
    if not evidence:
        raise RecordError('MISSING_SOURCE_EVIDENCE_ID', 'source_evidence_id')
    if ident['identity_state'] == 'MISSING_STABLE_ID':
        warnings.add('MISSING_STABLE_ID')

    value = row.get('grade_value', row.get('score'))
    if isinstance(value, bool) or (value is not None and not isinstance(value, (int, float))):
        raise RecordError('GRADE_VALUE_TYPE', 'grade_value')
    text = _first(row.get('grade_text'), row.get('grade'))
    if len(text) > 80:
        raise RecordError('GRADE_TEXT_TOO_LONG', 'grade_text')
    if text and value is not None:
        # An explicit text plus a numeric value may both be source values, but
        # a text that merely repeats the number is redundant and ambiguous.
        if text == str(value):
            text = ''
    scale = domain_models.mapped_status(
        row.get('grade_scale', row.get('scale')), GRADE_SCALE_MAP, unknown='UNKNOWN')
    scale_value = scale['status'] if scale['mapping_state'] == 'MAPPED' else 'UNKNOWN'
    if scale_value == 'UNKNOWN' and row.get('grade_scale') not in (None, ''):
        warnings.add('GRADE_SCALE_UNKNOWN:' + (scale['source_status_digest'] or ''))
    warnings = {warning for warning in warnings if warning}

    raw_state = row.get('state', row.get('status'))
    if raw_state in (None, ''):
        if value is not None or text:
            state = 'GRADED'
        elif row.get('withheld') is True:
            state = 'WITHHELD'
        elif row.get('unpublished') is True:
            state = 'UNGRADED'
            warnings.add('GRADE_NOT_PUBLISHED')
        else:
            state = 'UNGRADED'
            warnings.add('GRADE_VALUE_MISSING')
    else:
        mapped = domain_models.mapped_status(raw_state, GRADE_STATE_MAP, unknown='UNKNOWN')
        state = mapped['status']
        if mapped['source_status_digest']:
            warnings.add('GRADE_STATE_UNKNOWN:' + mapped['source_status_digest'])
        if state == 'UNGRADED' and row.get('unpublished') is not False:
            warnings.add('GRADE_NOT_PUBLISHED')
    if state == 'GRADED' and value is None and not text:
        raise RecordError('GRADE_VALUE_MISSING', 'grade_value')
    if state in ('UNGRADED', 'WITHHELD', 'UNKNOWN') and (value is not None or text):
        raise RecordError('INCONSISTENT_GRADE_STATE', 'state')

    credits = row.get('credits')
    if credits is not None:
        if isinstance(credits, bool) or not isinstance(credits, (int, float)) or credits < 0:
            raise RecordError('INVALID_CREDITS', 'credits')
    published_at = None
    raw_published = row.get('published_at')
    if raw_published not in (None, ''):
        parsed = domain_models.canonical_time(raw_published)
        if parsed['valid'] and parsed['date_precision'] != 'date':
            published_at = parsed['value']
        else:
            warnings.add('GRADE_PUBLISHED_AT_INVALID')
    return {
        'source_id': source_id,
        'source_record_id': record_id or None,
        'grade_uid': ident['uid'],
        'course_uid': association['course_uid'],
        'semester_id': semester or None,
        'grade_value': value,
        'grade_text': text or None,
        'grade_scale': scale_value,
        'credits': credits,
        'state': state,
        'published_at': published_at,
        'identity_state': ident['identity_state'],
        'source_evidence_id': evidence,
        'warnings': sorted(warnings),
        'synthetic': bool(synthetic),
    }


def _uid_key(record):
    for key in ('entry_uid', 'exam_uid', 'grade_uid', 'course_uid'):
        if key in record:
            return key
    return None


def _semantic(record):
    ignored = {'warnings', 'identity_state', 'dedupe_state', 'synthetic'}
    return {key: value for key, value in record.items() if key not in ignored}


def merge_records(records):
    """Keep distinct records; same UID with different data stays conflicted.

    A missing UID keeps the record with ``MISSING_STABLE_ID``.  Same title with
    different UIDs is reported as ``POSSIBLE_DUPLICATE_TITLE`` and never merged.
    """
    unique, groups, conflicts = {}, {}, set()
    warnings = set()
    for original in records or ():
        if not isinstance(original, dict):
            warnings.add('INVALID_RECORD')
            continue
        record = dict(original)
        key = _uid_key(record)
        uid = record.get(key) if key else None
        if key is None or uid is None:
            record['identity_state'] = 'MISSING_STABLE_ID'
            record['dedupe_state'] = 'NOT_DEDUPED'
            unique[id(record)] = record
            warnings.add('MISSING_STABLE_ID')
            continue
        if uid in conflicts:
            record[key] = None
            record['identity_state'] = 'CONFLICT'
            record['dedupe_state'] = 'NOT_DEDUPED'
            record['warnings'] = sorted(set(record.get('warnings') or ()) | {'SOURCE_RECORD_CONFLICT'})
            unique[id(record)] = record
            warnings.add('SOURCE_RECORD_CONFLICT')
            continue
        if uid not in groups:
            groups[uid] = record
            continue
        existing = groups[uid]
        if _semantic(existing) != _semantic(record):
            conflicts.add(uid)
            for item in (existing, record):
                item[key] = None
                item['identity_state'] = 'CONFLICT'
                item['dedupe_state'] = 'NOT_DEDUPED'
                item['warnings'] = sorted(set(item.get('warnings') or ()) | {'SOURCE_RECORD_CONFLICT'})
                unique[id(item)] = item
            groups.pop(uid, None)
            warnings.add('SOURCE_RECORD_CONFLICT')
    for record in groups.values():
        if record.get('identity_state') == 'STABLE':
            record['dedupe_state'] = 'UNIQUE'
        unique[id(record)] = record
    kept = list(unique.values())
    titles = {}
    for record in kept:
        key = _uid_key(record)
        if not key or not record.get(key):
            continue
        title = _text(record.get('title')).casefold()
        if title:
            titles.setdefault((record.get('source_id'), title), []).append(record)
    relations = []
    for (source, title), group in sorted(titles.items()):
        uids = sorted({record[_uid_key(record)] for record in group})
        if len(uids) < 2:
            continue
        for record in group:
            record['dedupe_state'] = 'SAME_TITLE_DIFFERENT_IDENTITY'
        relations.append({'kind': 'POSSIBLE_DUPLICATE_TITLE', 'source_id': source,
                          'title': group[0].get('title'), 'members': uids,
                          'semantics': 'SAME_TITLE_DIFFERENT_IDENTITY'})
        warnings.add('SAME_TITLE_DIFFERENT_IDENTITY')
    kept.sort(key=lambda record: tuple(str(record.get(key) or '') for key in
                                       ('entry_uid', 'exam_uid', 'grade_uid', 'course_uid')))
    return {'records': kept, 'relations': relations, 'warnings': sorted(warnings)}


def normalize_rows(rows, normalizer, **options):
    """Apply a row normaliser, keeping failures as codes and counts."""
    return domain_models.normalize_records(rows, normalizer, **options)


__all__ = [
    'CourseIndex', 'EXAM_STATE_MAP', 'GRADE_SCALE_MAP', 'GRADE_STATE_MAP',
    'RELEASE_STATE_MAP', 'VERSION', 'merge_records', 'normalize_exam',
    'normalize_grade', 'normalize_rows', 'normalize_semester',
    'normalize_timetable_entry', 'select_semester',
]
