"""Pure normalisation helpers for the frozen v0.2 domain models.

Adapters read a source and map fields; this module owns identity, time and
enum semantics that must be identical across every adapter.  Nothing here
performs I/O, reads credentials or decides that a source read succeeded.

Rules implemented here (see contracts/v0.2/sources/data-models and
docs/contracts/data-models.md):

* stable identity never comes from a title, teacher name, room name, array
  position or display order;
* a missing stable identifier yields ``uid=None`` and
  ``identity_state=MISSING_STABLE_ID`` rather than a fabricated identifier;
* the same title with different source identifiers keeps both records and
  reports a possible-duplicate relation;
* a date-only value stays a date and is never padded to midnight;
* an unfamiliar enum keeps ``UNKNOWN`` plus a non-reversible digest, never a
  neighbouring value and never the raw label in a public warning.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import re

VERSION = 'domain-models-v1'
TIMEZONE_NAME = 'Asia/Shanghai'
TZ = timezone(timedelta(hours=8))

SOURCE_ID = re.compile(r'^[a-z][a-z0-9_.-]{2,120}$')
UID = re.compile(r'^[a-z][a-z0-9_.-]{1,80}:[A-Z]+:[A-Za-z0-9_.:+-]{1,180}$')
PART = re.compile(r'^[A-Za-z0-9_.:+-]{1,240}$')
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

KINDS = {
    'COURSE', 'RESOURCE', 'GRADE', 'ORDER', 'ATTACHMENT', 'ASSIGNMENT',
    'SUBMISSION', 'RENEWAL', 'EVENT',
}
UID_FIELDS = {
    'COURSE': 'course_uid', 'RESOURCE': 'resource_uid', 'GRADE': 'grade_uid',
    'ORDER': 'order_uid', 'ATTACHMENT': 'attachment_uid',
    'ASSIGNMENT': 'assignment_uid', 'SUBMISSION': 'submission_uid',
    'RENEWAL': 'renewal_uid', 'EVENT': 'event_uid',
}

ROLE_MAP = {
    'student': 'STUDENT', '学生': 'STUDENT',
    'teacher': 'TEACHER', '教师': 'TEACHER', '老师': 'TEACHER',
}


class RecordError(ValueError):
    """A record cannot be represented under the frozen model."""

    def __init__(self, code, field=None):
        self.code = code
        self.field = field
        super().__init__(f'{code}:{field}' if field else code)


def _text(value):
    if value is None:
        return ''
    text = html.unescape(str(value)).strip()
    return CONTROL.sub('', text)


def _first_text(*values):
    for value in values:
        text = _text(value)
        if text:
            return text
    return ''


def require_title(value, field='title'):
    text = _text(value)
    if not text:
        raise RecordError('MISSING_TITLE', field)
    return text[:500]


def _part(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not text or len(text) > 240 or not PART.fullmatch(text):
        return None
    return text


def identity(source_id, kind, *parts):
    """Return ``uid``/``identity_state`` for a record identity.

    ``parts`` are the semantic scope components, never a display title.  A
    missing or malformed component yields ``MISSING_STABLE_ID``.
    """
    if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id) or kind not in KINDS:
        return {'uid': None, 'identity_state': 'MISSING_STABLE_ID'}
    values = [_part(part) for part in parts]
    if not values or any(value is None for value in values):
        return {'uid': None, 'identity_state': 'MISSING_STABLE_ID'}
    uid = ':'.join([source_id, kind, *values])
    if len(uid) > 280 or not UID.fullmatch(uid):
        return {'uid': None, 'identity_state': 'MISSING_STABLE_ID'}
    return {'uid': uid, 'identity_state': 'STABLE'}


def course_uid(source_id, semester_id, source_record_id):
    return identity(source_id, 'COURSE', semester_id, source_record_id)


def resource_uid(source_id, source_record_id):
    return identity(source_id, 'RESOURCE', source_record_id)


def grade_uid(source_id, semester_id, source_record_id):
    return identity(source_id, 'GRADE', semester_id, source_record_id)


def order_uid(source_id, source_record_id):
    return identity(source_id, 'ORDER', source_record_id)


def attachment_uid(source_id, source_record_id):
    return identity(source_id, 'ATTACHMENT', source_record_id)


def assignment_uid(source_id, source_record_id):
    return identity(source_id, 'ASSIGNMENT', source_record_id)


def submission_uid(source_id, assignment_or_record_id, version):
    return identity(source_id, 'SUBMISSION', assignment_or_record_id, version)


def renewal_uid(source_id, loan_uid):
    """``<source_id>:RENEWAL:<loan_uid>``; an existing prefix is not doubled."""
    if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
        return {'uid': None, 'identity_state': 'MISSING_STABLE_ID'}
    text = _part(loan_uid)
    if text is None:
        return {'uid': None, 'identity_state': 'MISSING_STABLE_ID'}
    prefix = source_id + ':RENEWAL:'
    if text.startswith(prefix):
        text = text[len(prefix):]
    return identity(source_id, 'RENEWAL', text)


def event_uid(account_domain, service_domain, kind, record_id, occurrence='0'):
    """Compose a calendar-event-v1 UID from non-display fields."""
    pieces = [account_domain, service_domain, kind, record_id, occurrence]
    if any(not _part(piece) for piece in pieces):
        return None
    uid = ':'.join(str(piece) for piece in pieces)
    return uid if re.fullmatch(r'^[A-Za-z0-9_.:+-]{1,240}$', uid) else None


def canonical_time(value, *, timezone_name=TIMEZONE_NAME):
    """Normalise a source time without inventing precision.

    Returns ``value``/``date_precision``/``timezone``/``inferred_timezone``/
    ``valid``.  Date-only input stays ``YYYY-MM-DD`` with precision ``date``.
    Naive date-time input is treated as platform-local Asia/Shanghai and is
    flagged as inferred; offset-aware input is converted to +08:00.
    """
    result = {'value': None, 'date_precision': 'unknown',
              'timezone': timezone_name, 'inferred_timezone': False, 'valid': False}
    if value is None or value == '':
        return result
    if isinstance(value, datetime):
        text = value.isoformat()
    elif isinstance(value, date):
        return {**result, 'value': value.isoformat(), 'date_precision': 'date', 'valid': True}
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(int(value))
    elif isinstance(value, str):
        text = value.strip()
    else:
        return result
    if not text:
        return result
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        try:
            return {**result, 'value': date.fromisoformat(text).isoformat(),
                    'date_precision': 'date', 'valid': True}
        except ValueError:
            return result
    if re.fullmatch(r'[1-9]\d{12}', text):
        return {**result, 'value': datetime.fromtimestamp(int(text) / 1000, TZ).isoformat(timespec='milliseconds'),
                'date_precision': 'millisecond', 'valid': True}
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?', text):
        return result
    inferred = not re.search(r'(?:Z|[+-]\d{2}:\d{2})$', text)
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return result
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    fraction = re.search(r'\.(\d+)', text)
    if fraction:
        digits = len(fraction.group(1))
        precision = 'millisecond' if digits <= 3 else 'microsecond'
    elif re.search(r'[T ]\d{2}:\d{2}:\d{2}', text):
        precision = 'second'
    else:
        precision = 'minute'
    timespec = 'milliseconds' if precision == 'millisecond' else 'microseconds' if precision == 'microsecond' else 'seconds' if precision == 'second' else 'minutes'
    return {**result, 'value': parsed.astimezone(TZ).isoformat(timespec=timespec),
            'date_precision': precision, 'inferred_timezone': inferred, 'valid': True}


def sort_key(value):
    """A chronological key that does not turn a date-only value into midnight.

    Date-only records sort before timed records on the same day; unknown values
    sort last.  The tuple never compares a date-only value with an invented
    ``00:00`` instant, so exports and ordering keep the precision distinction.
    """
    parsed = canonical_time(value)
    if parsed['date_precision'] == 'date' and parsed['value']:
        return (parsed['value'], 0, '')
    if parsed['valid'] and parsed['value']:
        return (parsed['value'][:10], 1, parsed['value'][11:])
    return ('9999-12-31', 2, '')


def calendar_time(start, end=None):
    """Build a calendar-event-v1 ``time`` object or ``None``."""
    first = canonical_time(start)
    if not first['valid']:
        return None
    if first['date_precision'] == 'date':
        return {'kind': 'DATE', 'date': first['value'], 'timezone': TIMEZONE_NAME}
    last = canonical_time(end) if end not in (None, '') else None
    if last is not None and last['valid'] and last['date_precision'] != 'date':
        if last['value'] < first['value']:
            raise RecordError('END_BEFORE_START', 'end')
        end_value = last['value']
    else:
        end_value = None
    return {'kind': 'DATE_TIME', 'start': first['value'], 'end': end_value,
            'timezone': TIMEZONE_NAME}


def normalize_role(value):
    """Map course roles to ``STUDENT``/``TEACHER``/``UNKNOWN``."""
    role, digest = _enum(value, ROLE_MAP)
    return role, digest


def mapped_status(value, mapping, *, unknown='UNKNOWN'):
    """Map a platform status without publishing an unfamiliar raw label."""
    status, digest = _enum(value, mapping, unknown=unknown)
    return {'status': status, 'mapping_state': 'MAPPED' if digest is None else 'UNKNOWN',
            'source_status_digest': digest}


def _enum(value, mapping, *, unknown='UNKNOWN'):
    if isinstance(value, bool):
        return unknown, None
    if isinstance(value, int) and value in mapping:
        return mapping[value], None
    if isinstance(value, str):
        key = value.strip()
        if key in mapping:
            return mapping[key], None
        lowered = key.casefold()
        if lowered in mapping:
            return mapping[lowered], None
        if re.fullmatch(r'-?\d{1,12}', key):
            try:
                number = int(key)
            except ValueError:
                number = None
            if number is not None and number in mapping:
                return mapping[number], None
    return unknown, (hashlib.sha256(str(value).encode('utf-8', 'replace')).hexdigest()[:12]
                     if value not in (None, '') else None)


def standard_record(*, source_id, source_record_id, account_scope, checked_at,
                    date_precision='unknown', source_status=None, model,
                    payload, status_mapping='SOURCE_VALUE_RETAINED'):
    """Wrap a canonical model with the common v0.2 record metadata.

    The frozen model schemas do not all carry account scope and check time;
    this wrapper is the internal record shape that adapters pass around.  It
    never replaces the model-specific schema for public output.
    """
    if not isinstance(account_scope, str) or not account_scope:
        raise RecordError('MISSING_ACCOUNT_SCOPE', 'account_scope')
    if date_precision not in ('date', 'minute', 'second', 'millisecond', 'microsecond', 'unknown'):
        raise RecordError('INVALID_DATE_PRECISION', 'date_precision')
    checked = canonical_time(checked_at)
    if not checked['valid'] or checked['date_precision'] == 'date':
        raise RecordError('INVALID_CHECKED_AT', 'checked_at')
    return {
        'source_id': source_id,
        'source_record_id': source_record_id,
        'account_scope': account_scope,
        'checked_at': checked['value'],
        'date_precision': date_precision,
        'source_status': source_status,
        'status_mapping': status_mapping,
        'model': model,
        'data': payload,
    }


def normalize_course(row, *, source_id, semester_id, observed_at,
                     source_evidence_id, synthetic=True):
    """Normalise one course row to ``course-v1``.

    Missing title, a date-only/invalid observation time, an empty evidence id
    or an inconsistent identity fail closed with ``RecordError``.  The role is
    upper-cased (the current adapter returns lowercase ``student``), and an
    unfamiliar role becomes ``UNKNOWN`` with a digest warning, not ``STUDENT``.
    """
    if not isinstance(row, dict):
        raise RecordError('INVALID_RECORD', 'row')
    title = require_title(_first_text(row.get('course_name'), row.get('title')))
    record_id = _first_text(row.get('course_id'), row.get('id'), row.get('wlkcid'))[:240]
    semester = _first_text(semester_id, row.get('semester'))[:80]
    ident = course_uid(source_id, semester, record_id)
    role, role_digest = normalize_role(row.get('role'))
    teachers = row.get('teacher_names')
    if teachers is None:
        teachers = row.get('teacherName') or row.get('jsm')
    if isinstance(teachers, str):
        teachers = [teachers]
    if not isinstance(teachers, list):
        teachers = []
    teachers = [_text(value)[:200] for value in teachers if _text(value)][:40]
    observed = canonical_time(observed_at)
    if not observed['valid'] or observed['date_precision'] == 'date':
        raise RecordError('INVALID_OBSERVED_AT', 'observed_at')
    evidence_id = _text(source_evidence_id)[:120]
    if not evidence_id:
        raise RecordError('MISSING_SOURCE_EVIDENCE_ID', 'source_evidence_id')
    warnings = []
    if ident['identity_state'] == 'MISSING_STABLE_ID':
        warnings.append('MISSING_STABLE_ID')
    if role_digest:
        warnings.append('ROLE_UNKNOWN:' + role_digest)
    return {
        'source_id': source_id,
        'source_record_id': record_id[:240] or None,
        'course_uid': ident['uid'],
        'semester_id': semester[:80] or None,
        'title': title,
        'teacher_names': teachers,
        'role': role,
        'identity_state': ident['identity_state'],
        'dedupe_state': 'NOT_DEDUPED',
        'observed_at': observed['value'],
        'source_evidence_id': evidence_id,
        'warnings': warnings,
        'synthetic': bool(synthetic),
    }


def _uid_key(record):
    for key in UID_FIELDS.values():
        if key in record:
            return key
    return None


def _semantic(record):
    ignored = {'dedupe_state', 'warnings', 'source_evidence_id', 'observed_at', 'synthetic',
               'identity_state'}
    return {key: value for key, value in record.items() if key not in ignored}


def deduplicate(records):
    """Group canonical records without merging different source identities.

    Returns ``{'records': [...], 'relations': [...], 'warnings': [...]}``.
    Same UID with different fields becomes ``CONFLICT`` and is retained; the
    same title with different UIDs keeps both records and reports a
    ``POSSIBLE_DUPLICATE_TITLE`` relation.
    """
    unique = {}
    groups = {}
    conflicts = set()
    warnings = set()
    for original in records:
        if not isinstance(original, dict):
            warnings.add('INVALID_RECORD')
            continue
        record = dict(original)
        key = _uid_key(record)
        uid = record.get(key) if key else None
        if key is None:
            record['identity_state'] = 'MISSING_STABLE_ID'
            record.setdefault('dedupe_state', 'NOT_DEDUPED')
            unique[id(record)] = record
            warnings.add('MISSING_STABLE_ID')
            continue
        if uid is None:
            record['identity_state'] = 'MISSING_STABLE_ID'
            record.setdefault('dedupe_state', 'NOT_DEDUPED')
            unique[id(record)] = record
            warnings.add('MISSING_STABLE_ID')
            continue
        if uid in conflicts:
            record[key] = None
            record['identity_state'] = 'CONFLICT'
            record['dedupe_state'] = 'NOT_DEDUPED'
            record['warnings'] = sorted(set(record.get('warnings', []) + ['SOURCE_RECORD_CONFLICT']))
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
                item['warnings'] = sorted(set(item.get('warnings', []) + ['SOURCE_RECORD_CONFLICT']))
            # Keep both copies observable even though their UID is no longer
            # published; the conflict itself is the evidence.
            unique[id(existing)] = existing
            unique[id(record)] = record
            groups.pop(uid, None)
            warnings.add('SOURCE_RECORD_CONFLICT')
    for record in groups.values():
        if record.get('identity_state') == 'STABLE':
            record['dedupe_state'] = 'UNIQUE'
        unique[id(record)] = record

    kept = list(unique.values())
    titles = {}
    for record in kept:
        uid_key = _uid_key(record)
        if not uid_key or not record.get(uid_key):
            continue
        title = _text(record.get('title')).casefold()
        source = record.get('source_id')
        if title:
            titles.setdefault((source, title), []).append(record)
    relations = []
    for (source, title), group in sorted(titles.items()):
        uids = sorted({record[_uid_key(record)] for record in group})
        if len(uids) < 2:
            continue
        for record in group:
            record['dedupe_state'] = 'SAME_TITLE_DIFFERENT_IDENTITY'
        relations.append({
            'kind': 'POSSIBLE_DUPLICATE_TITLE',
            'source_id': source,
            'title': group[0].get('title'),
            'members': uids,
            'semantics': 'SAME_TITLE_DIFFERENT_IDENTITY',
        })
        warnings.add('SAME_TITLE_DIFFERENT_IDENTITY')
    kept.sort(key=lambda record: tuple(str(record.get(key) or '') for key in UID_FIELDS.values()))
    return {'records': kept, 'relations': relations, 'warnings': sorted(warnings)}


def normalize_records(rows, normalizer, **options):
    """Apply a row normaliser, keeping failures as counts and safe codes."""
    records, rejected, warnings = [], [], set()
    for row in rows or ():
        try:
            records.append(normalizer(row, **options))
        except RecordError as error:
            rejected.append({'code': error.code, 'field': error.field})
            warnings.add(error.code)
        except (TypeError, KeyError, ValueError):
            rejected.append({'code': 'INVALID_SOURCE_RECORD', 'field': None})
            warnings.add('INVALID_SOURCE_RECORD')
    return {'records': records, 'rejected': rejected,
            'rejected_count': len(rejected), 'warnings': sorted(warnings)}
