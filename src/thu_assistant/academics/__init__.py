"""Read-only personal timetable, exam and course-grade adapters (module 4.1).

The package owns the academic record boundary:

* ``CAPABILITIES`` / ``describe`` record the enablement state of the three
  capabilities.  All three are ``BLOCKED_SOURCE_EVIDENCE``; ``AcademicsReader``
  returns an explicit blocked result and never a simulated record.
* ``models`` normalises semesters, course associations, timetable entries,
  exams and ``grade-v1`` grades; ``rules`` expands weekly patterns only with an
  explicit school calendar and period table; ``events`` converts timetable
  occurrences and exams to ``calendar-event-v1`` and refuses grades.
* ``sensitivity`` keeps course grades out of caches, logs, public summaries,
  model requests and todos unless the caller asks explicitly.

Nothing here performs I/O, reads credentials or registers a public operation;
6.1 owns the business facade and MCP/CLI wiring.
"""
from __future__ import annotations

from . import events, models, rules, sensitivity
from .errors import CODES, AcademicsError, error_object
from .events import calendar_events, exam_event, timetable_events
from .models import (
    CourseIndex,
    merge_records,
    normalize_exam,
    normalize_grade,
    normalize_rows,
    normalize_semester,
    normalize_timetable_entry,
    select_semester,
)
from .reader import (
    CAPABILITIES,
    AcademicsReader,
    blocked_result,
    capability_report,
    collect_pages,
    describe,
    operation_schemas,
    read_synthetic,
    validate_parameters,
)
from .rules import PeriodTable, SchoolCalendar, expand, normalize_week_pattern

__all__ = [
    'AcademicsError',
    'AcademicsReader',
    'CAPABILITIES',
    'CODES',
    'CourseIndex',
    'PeriodTable',
    'SchoolCalendar',
    'blocked_result',
    'calendar_events',
    'capability_report',
    'collect_pages',
    'describe',
    'error_object',
    'events',
    'exam_event',
    'expand',
    'merge_records',
    'models',
    'normalize_exam',
    'normalize_grade',
    'normalize_rows',
    'normalize_semester',
    'normalize_timetable_entry',
    'normalize_week_pattern',
    'operation_schemas',
    'read_synthetic',
    'rules',
    'select_semester',
    'sensitivity',
    'timetable_events',
    'validate_parameters',
]
