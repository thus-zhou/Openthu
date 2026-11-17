"""Privacy policy for course grades (4.1).

Course grades are personal academic records.  They are readable only when the
caller asks for them explicitly, never enter a plain cache, a public dashboard
summary, a query log or a model request by default.  This module owns those
rules and keeps them independent from transport and storage, so a later
facade (6.1) or dashboard (6.2) cannot re-implement them differently.

The functions here never copy ``grade_value``/``grade_text`` into an audit or
summary payload.  ``model_context`` is the only path that may include values,
and only with an explicit request and a registered purpose.
"""
from __future__ import annotations

from .errors import AcademicsError

VERSION = 'academics-grade-privacy-v1'

POLICY = {
    'cache': 'ENCRYPTED_STORE_ONLY',
    'audit_log': 'COUNTS_ONLY',
    'public_dashboard': 'EXCLUDED',
    'model_context': 'EXPLICIT_REQUEST_ONLY',
    'calendar_or_todo': 'EXCLUDED',
}

PURPOSES = {
    'USER_REQUESTED_GRADE_READ',
    'USER_REQUESTED_ACADEMIC_REVIEW',
    'SYNTHETIC_FIXTURE',
}

_SENSITIVE_KEYS = {'grade_value', 'grade_text', 'score', 'grade'}


def _grade_records(records):
    return [row for row in records or () if isinstance(row, dict)]


def state_counts(records):
    counts = {}
    for row in _grade_records(records):
        state = row.get('state') if row.get('state') in ('GRADED', 'UNGRADED', 'WITHHELD', 'UNKNOWN') else 'UNKNOWN'
        counts[state] = counts.get(state, 0) + 1
    return dict(sorted(counts.items()))


def audit_record(*, semester_id, records, checked_at, source_id='academics.grades',
                 explicit_request=True):
    """A query-log entry with counts only, never a value or a course title."""
    return {
        'capability': 'grades',
        'source_id': source_id,
        'semester_id': semester_id or None,
        'checked_at': checked_at,
        'row_count': len(_grade_records(records)),
        'state_counts': state_counts(records),
        'explicit_request': bool(explicit_request),
        'values_included': False,
    }


def dashboard_summary(*, semester_id, records):
    """Counts for a local board; grades are excluded from public summaries."""
    return {
        'capability': 'grades',
        'semester_id': semester_id or None,
        'row_count': len(_grade_records(records)),
        'state_counts': state_counts(records),
        'values_included': False,
        'reason': 'GRADES_EXCLUDED_FROM_PUBLIC_SUMMARY',
    }


def model_context(*, records, explicit_request=False, purpose=None):
    """The only grade path that may carry values into a model request."""
    if not explicit_request:
        return {'included': False, 'reason': 'GRADES_REQUIRE_EXPLICIT_REQUEST', 'items': []}
    if purpose not in PURPOSES - {'SYNTHETIC_FIXTURE'}:
        raise AcademicsError('ACADEMICS_SENSITIVE_REQUEST_REQUIRED', 'purpose')
    items = []
    for row in _grade_records(records):
        items.append({
            'course_uid': row.get('course_uid'),
            'semester_id': row.get('semester_id'),
            'state': row.get('state'),
            'grade_scale': row.get('grade_scale'),
            'grade_value': row.get('grade_value'),
            'grade_text': row.get('grade_text'),
            'credits': row.get('credits'),
        })
    return {'included': True, 'purpose': purpose, 'items': items}


def require_encrypted_cache(encrypted):
    """A plain cache for grades is refused; only the encrypted store may hold them."""
    if encrypted is not True:
        raise AcademicsError('ACADEMICS_GRADE_CACHE_FORBIDDEN')
    return True


def contains_grade_values(payload):
    """True when a payload carries a non-null raw grade value or text."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _SENSITIVE_KEYS and value not in (None, ''):
                return True
            if contains_grade_values(value):
                return True
    elif isinstance(payload, (list, tuple)):
        return any(contains_grade_values(item) for item in payload)
    return False


def describe():
    return {'version': VERSION, 'policy': dict(POLICY), 'purposes': sorted(PURPOSES),
            'rules': [
                'grades require an explicit per-query request',
                'grades are excluded from public dashboard summaries, logs and todos',
                'a grade cache must be encrypted; a plain JSON cache is refused',
                'model context includes grade values only with explicit_request and a registered purpose',
            ]}


__all__ = [
    'POLICY', 'PURPOSES', 'VERSION', 'audit_record', 'contains_grade_values',
    'dashboard_summary', 'describe', 'model_context', 'require_encrypted_cache',
    'state_counts',
]
