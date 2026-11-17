"""School calendar, period table and week expansion for academics (4.1).

Only an explicitly checked calendar plus an explicit period table may turn a
weekly course pattern into concrete dates and clock times.  Anything shorter —
a missing term start, an unknown parity, a missing section table — keeps the
raw cycle representation and an honest derivation state instead of a guessed
date or a fabricated ``00:00``.

Rules implemented here (``week-expansion-v1``):

* week numbers are 1-based blocks of seven days counted from the declared term
  start; the block start is not assumed to be a Monday;
* parity is only ``ALL``, ``ODD`` or ``EVEN``; any other label is unknown and
  blocks expansion;
* a holiday removes an occurrence and is reported in ``skipped``;
* a makeup day (调课) declares an effective weekday for one date and may list
  the dates it replaces; it never creates a date for an unregistered weekday;
* dates outside the declared teaching weeks are not invented, they are
  reported as skipped with the original week retained.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import re

from .. import domain_models
from .errors import AcademicsError

VERSION = 'academics-rules-v1'
CALENDAR_VERSION = 'school-calendar-v1'
PERIOD_TABLE_VERSION = 'period-table-v1'
EXPANSION_RULE = 'week-expansion-v1'

_TIME = re.compile(r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

WEEKDAY_NAMES = {
    'mon': 1, 'monday': 1, '周一': 1,
    'tue': 2, 'tuesday': 2, '周二': 2,
    'wed': 3, 'wednesday': 3, '周三': 3,
    'thu': 4, 'thursday': 4, '周四': 4,
    'fri': 5, 'friday': 5, '周五': 5,
    'sat': 6, 'saturday': 6, '周六': 6,
    'sun': 7, 'sunday': 7, '周日': 7,
}
PARITY = {
    'all': 'ALL', 'every': 'ALL', 'weekly': 'ALL', '每周': 'ALL', '全部': 'ALL',
    'odd': 'ODD', 'single': 'ODD', '单': 'ODD', '单周': 'ODD',
    'even': 'EVEN', 'double': 'EVEN', '双': 'EVEN', '双周': 'EVEN',
}
EXPANSION_STATES = {
    'EXPANDED', 'EXPANDED_DATE_ONLY', 'PARTIAL', 'RAW_CYCLE_ONLY',
    'CALENDAR_MISMATCH', 'INCOMPLETE_PATTERN', 'NO_OCCURRENCES',
}


def _parse_date(value, *, field='date'):
    if isinstance(value, datetime):
        raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', field)
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not _DATE.fullmatch(value.strip()):
        raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', field)
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', field) from None


def _parse_weekday(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1 <= value <= 7:
        return value
    if isinstance(value, str):
        text = value.strip().casefold()
        if text.isdigit() and 1 <= int(text) <= 7:
            return int(text)
        return WEEKDAY_NAMES.get(text)
    return None


def _int(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if 1 <= number <= 60 else None


class PeriodTable:
    """Explicit section start/end times for one semester."""

    def __init__(self, semester_id, periods):
        if not isinstance(semester_id, str) or not semester_id.strip():
            raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'period_table.semester_id')
        if not isinstance(periods, (list, tuple)) or not periods:
            raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'period_table.periods')
        table = {}
        for row in periods:
            if not isinstance(row, dict):
                raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'period_table.row')
            number = _int(row.get('period', row.get('index')))
            start = row.get('start')
            end = row.get('end')
            if number is None or number > 20 or not isinstance(start, str) or not isinstance(end, str):
                raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'period_table.row')
            if not _TIME.fullmatch(start) or not _TIME.fullmatch(end) or end <= start:
                raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'period_table.times')
            if number in table:
                raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'period_table.duplicate')
            table[number] = {'period': number, 'start': start, 'end': end}
        self.semester_id = semester_id.strip()
        self.periods = dict(sorted(table.items()))

    @classmethod
    def from_mapping(cls, row):
        if not isinstance(row, dict):
            raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'period_table')
        return cls(row.get('semester_id'), row.get('periods'))

    def bounds(self, numbers):
        """Return ``{'start', 'end'}`` for the given section numbers, or None."""
        values = [_int(number) for number in numbers or ()]
        if not values or any(value is None or value not in self.periods for value in values):
            return None
        selected = [self.periods[value] for value in sorted(set(values))]
        return {'start': selected[0]['start'], 'end': selected[-1]['end']}


class SchoolCalendar:
    """Explicit teaching calendar for one semester."""

    def __init__(self, semester_id, term_start, teaching_weeks, holidays=(), makeup_days=()):
        if not isinstance(semester_id, str) or not semester_id.strip():
            raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'calendar.semester_id')
        weeks = _int(teaching_weeks)
        if weeks is None or weeks > 52:
            raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'calendar.teaching_weeks')
        self.semester_id = semester_id.strip()
        self.term_start = _parse_date(term_start, field='calendar.term_start')
        self.teaching_weeks = weeks
        self.holidays = set()
        for item in holidays or ():
            value = item.get('date') if isinstance(item, dict) else item
            self.holidays.add(_parse_date(value, field='calendar.holidays'))
        self.makeup_days = []
        self._suspended = set()
        for item in makeup_days or ():
            if not isinstance(item, dict):
                raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'calendar.makeup_days')
            weekday = _parse_weekday(item.get('weekday'))
            if weekday is None:
                raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'calendar.makeup_weekday')
            day = _parse_date(item.get('date'), field='calendar.makeup_date')
            replaces = tuple(_parse_date(value, field='calendar.makeup_replaces')
                             for value in item.get('replaces') or ())
            self.makeup_days.append({'date': day, 'weekday': weekday, 'replaces': replaces})
            self._suspended.update(replaces)

    @classmethod
    def from_mapping(cls, row):
        if not isinstance(row, dict):
            raise AcademicsError('ACADEMICS_CALENDAR_MISMATCH', 'calendar')
        return cls(row.get('semester_id'), row.get('term_start'), row.get('teaching_weeks'),
                   row.get('holidays') or (), row.get('makeup_days') or ())

    def week_of(self, day):
        return (day - self.term_start).days // 7 + 1

    def date_for(self, week, weekday):
        offset = (weekday - self.term_start.isoweekday()) % 7
        return self.term_start + timedelta(days=(week - 1) * 7 + offset)

    def describe(self):
        return {
            'version': CALENDAR_VERSION,
            'semester_id': self.semester_id,
            'term_start': self.term_start.isoformat(),
            'teaching_weeks': self.teaching_weeks,
            'holidays': sorted(day.isoformat() for day in self.holidays),
            'makeup_days': [
                {'date': row['date'].isoformat(), 'weekday': row['weekday'],
                 'replaces': [day.isoformat() for day in row['replaces']]}
                for row in self.makeup_days
            ],
        }


def _week_list(row):
    explicit = row.get('weeks')
    if isinstance(explicit, (list, tuple)):
        numbers = [_int(value) for value in explicit]
        if not numbers or any(value is None for value in numbers):
            return []
        return sorted(set(numbers))
    start = _int(row.get('start_week', row.get('week_start')))
    end = _int(row.get('end_week', row.get('week_end')))
    if start is None or end is None or end < start:
        return []
    return list(range(start, end + 1))


def normalize_week_pattern(row):
    """Normalise a weekly pattern without expanding it.

    Returns ``weeks``, ``parity``, ``weekday``, ``periods``, ``date_range``,
    a safe ``raw`` text, ``complete`` and ``warnings``.  An unknown parity or a
    missing weekday makes ``complete`` false; callers must not expand then.
    """
    if not isinstance(row, dict):
        raise AcademicsError('ACADEMICS_INVALID_WEEK_PATTERN', 'pattern')
    warnings = []
    weeks = _week_list(row)
    if not weeks:
        warnings.append('WEEK_RANGE_MISSING')
    parity_value = row.get('parity', row.get('week_type'))
    if parity_value in (None, ''):
        parity = 'ALL'
    else:
        parity = PARITY.get(str(parity_value).strip().casefold(), 'UNKNOWN')
        if parity == 'UNKNOWN':
            warnings.append('WEEK_PARITY_UNKNOWN')
    weekday = _parse_weekday(row.get('weekday', row.get('day_of_week')))
    if weekday is None:
        warnings.append('WEEKDAY_MISSING')
    periods = []
    raw_periods = row.get('periods')
    if raw_periods is None:
        raw_periods = []
        start = _int(row.get('period_start'))
        end = _int(row.get('period_end'))
        if start is not None and end is not None and end >= start:
            raw_periods = list(range(start, end + 1))
    if isinstance(raw_periods, (list, tuple)):
        periods = sorted({value for value in (_int(item) for item in raw_periods) if value})
        if len(periods) != len([value for value in raw_periods if value not in (None, '')]):
            warnings.append('PERIODS_PARTIALLY_INVALID')
    else:
        warnings.append('PERIODS_INVALID')
    date_range = None
    raw_range = row.get('date_range')
    if isinstance(raw_range, dict):
        start = raw_range.get('start')
        end = raw_range.get('end')
        if isinstance(start, str) and _DATE.fullmatch(start) and isinstance(end, str) and _DATE.fullmatch(end):
            if end >= start:
                date_range = {'start': start, 'end': end}
        if date_range is None:
            warnings.append('DATE_RANGE_INVALID')
    raw = ' '.join(str(row.get(key, '')).strip() for key in
                   ('weeks', 'start_week', 'end_week', 'parity', 'weekday', 'periods'))
    complete = bool(weeks) and weekday is not None and parity != 'UNKNOWN'
    return {
        'weeks': weeks, 'parity': parity, 'weekday': weekday,
        'periods': periods, 'date_range': date_range,
        'raw': raw[:200], 'complete': complete, 'warnings': sorted(set(warnings)),
    }


def expand(pattern, calendar=None, period_table=None, *,
           semester_id=None, location=None):
    """Expand a normalised week pattern under explicit rules.

    ``calendar``/``period_table`` may be objects or mappings.  When either is
    absent, or the semester does not match, no date is produced.  A missing
    period table keeps the dates but leaves every clock time ``None``.
    """
    warnings = list(pattern.get('warnings') or ())
    derivation = {
        'kind': 'DERIVED',
        'rule_version': EXPANSION_RULE,
        'inputs': ['week_pattern'],
        'source_of_dates': None,
        'source_of_times': None,
        'not_a_source_fact': True,
    }
    result = {
        'state': 'RAW_CYCLE_ONLY',
        'pattern': {key: pattern.get(key) for key in
                    ('weeks', 'parity', 'weekday', 'periods', 'date_range', 'raw')},
        'occurrences': [], 'skipped': [],
        'warnings': warnings, 'derivation': derivation,
    }
    if not pattern.get('complete'):
        result['state'] = 'INCOMPLETE_PATTERN'
        result['warnings'] = sorted(set(warnings) | {'CYCLE_NOT_EXPANDED_INCOMPLETE'})
        return result
    if calendar is not None and isinstance(calendar, dict):
        calendar = SchoolCalendar.from_mapping(calendar)
    if period_table is not None and isinstance(period_table, dict):
        period_table = PeriodTable.from_mapping(period_table)
    expected_semester = semester_id or pattern.get('semester_id')
    for source, label in ((calendar, 'calendar'), (period_table, 'period_table')):
        if source is None:
            continue
        if expected_semester and source.semester_id != expected_semester:
            result['state'] = 'CALENDAR_MISMATCH'
            result['warnings'] = sorted(set(warnings) | {'SEMESTER_MISMATCH:' + label.upper()})
            return result
    if calendar is None:
        result['warnings'] = sorted(set(warnings) | {'CALENDAR_UNAVAILABLE'})
        return result
    derivation['source_of_dates'] = CALENDAR_VERSION
    if period_table is not None:
        derivation['source_of_times'] = PERIOD_TABLE_VERSION
    else:
        result['warnings'] = sorted(set(warnings) | {'PERIOD_TABLE_MISSING'})

    selected = [week for week in pattern['weeks']
                if pattern['parity'] == 'ALL'
                or (pattern['parity'] == 'ODD' and week % 2 == 1)
                or (pattern['parity'] == 'EVEN' and week % 2 == 0)]
    weekday = pattern['weekday']
    candidates = {calendar.date_for(week, weekday) for week in selected}
    for makeup in calendar.makeup_days:
        if makeup['weekday'] == weekday:
            candidates.add(makeup['date'])
    occurrences, skipped = [], []
    for day in sorted(candidates):
        week = calendar.week_of(day)
        if day in calendar._suspended:
            skipped.append({'date': day.isoformat(), 'week': week, 'reason': 'REPLACED_BY_MAKEUP_DAY'})
            continue
        if day in calendar.holidays:
            skipped.append({'date': day.isoformat(), 'week': week, 'reason': 'HOLIDAY'})
            continue
        if week < 1 or week > calendar.teaching_weeks:
            skipped.append({'date': day.isoformat(), 'week': week, 'reason': 'OUTSIDE_TEACHING_WEEKS'})
            continue
        if week not in selected:
            skipped.append({'date': day.isoformat(), 'week': week, 'reason': 'WEEK_NOT_SELECTED'})
            continue
        bounds = period_table.bounds(pattern['periods']) if period_table is not None else None
        if bounds is None:
            time_value = None
            time_state = ('PERIOD_TABLE_MISSING' if period_table is None
                          else 'PERIODS_MISSING')
        else:
            time_value = domain_models.calendar_time(
                f'{day.isoformat()}T{bounds["start"]}:00', f'{day.isoformat()}T{bounds["end"]}:00')
            time_state = 'PERIOD_TABLE'
        occurrences.append({
            'date': day.isoformat(), 'week': week, 'weekday': weekday,
            'periods': list(pattern['periods']),
            'start': bounds['start'] if bounds else None,
            'end': bounds['end'] if bounds else None,
            'time': time_value, 'time_state': time_state,
            'location': location,
        })
    result['occurrences'] = occurrences
    result['skipped'] = skipped
    reasons = {row['reason'] for row in skipped}
    if not occurrences and not skipped:
        result['state'] = 'NO_OCCURRENCES'
    elif skipped:
        result['state'] = 'PARTIAL'
    elif occurrences and all(row['time'] is not None for row in occurrences):
        result['state'] = 'EXPANDED'
    else:
        result['state'] = 'EXPANDED_DATE_ONLY'
    result['warnings'] = sorted(set(result['warnings']) |
                                {'SKIPPED_' + reason for reason in reasons})
    return result


__all__ = [
    'CALENDAR_VERSION', 'EXPANSION_RULE', 'EXPANSION_STATES', 'PARITY',
    'PERIOD_TABLE_VERSION', 'PeriodTable', 'SchoolCalendar', 'VERSION',
    'expand', 'normalize_week_pattern',
]
