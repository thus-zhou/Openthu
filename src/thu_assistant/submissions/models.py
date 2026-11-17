"""Models for the assignment submission boundary.

Two state axes are deliberately separate:

* ``STAGES`` is the local pipeline (what Openthu itself has done and observed).
* ``REMOTE_STATES`` is the frozen ``submission-v1`` enum, only ever filled in
  from an observation made on the platform.

``submission-v1`` requires ``SUBMITTED`` to carry ``is_final=true``,
``result_observed=true`` and a ``remote_submission_id``; a local "final submit
was sent, the result is not back yet" therefore stays ``SUBMIT_SENT_UNVERIFIED``
on the stage axis and never becomes a ``SUBMITTED`` record.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re

from .. import source_state
from .errors import SubmissionError

VERSION = 'learn-submission-v1'
SOURCE_ID = 'learn.submission'
ASSIGNMENT_SOURCE_ID = 'learn.assignments'

STAGES = (
    'PREPARED_LOCAL',
    'UPLOADING',
    'UPLOADED_STAGING',
    'DRAFT_SAVED',
    'SUBMIT_SENT_UNVERIFIED',
    'VERIFIED',
    'FAILED',
    'UNKNOWN',
)
REMOTE_STATES = (
    'PREPARED_LOCAL',
    'UPLOAD_STARTED',
    'UPLOADED_STAGING',
    'DRAFT_SAVED',
    'SUBMITTED',
    'SUPERSEDED',
    'FAILED',
    'UNKNOWN',
)
SUBMISSION_STATES = ('NONE', 'EXISTING', 'UNKNOWN')
REPLACEMENT_POLICIES = ('NO_OVERWRITE', 'REPLACE_EXISTING')

_ASSIGNMENT_ID = re.compile(r'[A-Za-z0-9_.:-]{1,180}')
_UID = re.compile(r'^[a-z][a-z0-9_.-]{1,80}:SUBMISSION:[A-Za-z0-9_.:-]{1,180}$')
_ASSIGNMENT_UID = re.compile(r'^[a-z][a-z0-9_.-]{1,80}:ASSIGNMENT:[A-Za-z0-9_.:-]{1,180}$')


def now_iso(clock=None):
    return source_state.now_iso(clock)


def _text(value, limit=4000):
    if isinstance(value, bool) or value is None:
        return ''
    if isinstance(value, (int, float)):
        value = str(value)
    return value.strip()[:limit] if isinstance(value, str) else ''


def _integer(value, minimum=0, maximum=1073741824):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if minimum <= value <= maximum else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if minimum <= parsed <= maximum else None
    return None


def _boolean(value):
    return value if isinstance(value, bool) else None


def _instant(value):
    """Return ``(datetime, precision)`` for a deadline-like value.

    ``NONE`` means the platform declares no deadline, ``UNVERIFIED`` means the
    value exists but cannot be read; the two are never merged.
    """
    text = _text(value, 40)
    if not text:
        return None, 'NONE'
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        try:
            day = datetime.strptime(text, '%Y-%m-%d').replace(tzinfo=source_state.TZ)
        except ValueError:
            return None, 'UNVERIFIED'
        # A date-only deadline is read as the end of that local day, and the
        # precision is reported so the caller never treats it as an instant.
        return day + timedelta(days=1) - timedelta(seconds=1), 'DATE'
    normalized = source_state.normalize_timestamp(text, None)
    if normalized is None:
        return None, 'UNVERIFIED'
    try:
        return datetime.fromisoformat(normalized), 'EXACT'
    except ValueError:
        return None, 'UNVERIFIED'


def normalize_assignment(payload, assignment_id):
    """Normalise a reader payload without inventing fields.

    Unknown or malformed values become ``None``/``UNKNOWN`` and add a warning;
    a malformed value is never read as "no submission" or "no deadline".
    """
    warnings = []
    if not isinstance(payload, dict):
        return None, ['ASSIGNMENT_UNREADABLE']
    reported = _text(payload.get('assignment_id') or payload.get('id'), 180)
    if reported and reported != assignment_id:
        return None, ['ASSIGNMENT_ID_MISMATCH']
    requested = _text(payload.get('requested'), 40)
    if requested == 'OFFICIAL_CONFIRMATION':
        warnings.append('OFFICIAL_CONFIRMATION_REQUIRED')

    deadline, deadline_precision = _instant(payload.get('deadline'))
    late_deadline, late_precision = _instant(payload.get('late_deadline'))
    if payload.get('deadline') and deadline_precision == 'UNVERIFIED':
        warnings.append('DEADLINE_UNVERIFIED')
    if payload.get('deadline') and deadline_precision == 'DATE':
        warnings.append('DEADLINE_DATE_PRECISION')
    if payload.get('late_deadline') and late_precision == 'UNVERIFIED':
        warnings.append('LATE_DEADLINE_UNVERIFIED')

    raw_extensions = payload.get('allowed_extensions')
    extensions = []
    if isinstance(raw_extensions, (list, tuple)):
        for item in raw_extensions:
            text = _text(item, 20).casefold()
            if text and not text.startswith('.'):
                text = '.' + text
            if re.fullmatch(r'\.[a-z0-9]{1,12}', text) and text not in extensions:
                extensions.append(text)
    elif raw_extensions:
        warnings.append('ALLOWED_EXTENSIONS_UNVERIFIED')

    max_bytes = _integer(payload.get('max_bytes'), 0, 1073741824)
    if payload.get('max_bytes') is not None and max_bytes is None:
        warnings.append('MAX_BYTES_UNVERIFIED')

    allow_multiple = _boolean(payload.get('allow_multiple_files'))
    if payload.get('allow_multiple_files') is not None and allow_multiple is None:
        warnings.append('MULTIPLE_FILES_RULE_UNVERIFIED')
        allow_multiple = True

    submitted = _boolean(payload.get('submitted'))
    submitted_at = _text(payload.get('submitted_at'), 40)
    version = _integer(payload.get('submission_version'), 0, 100000)
    if submitted is True or submitted_at:
        submission_state = 'EXISTING'
    elif submitted is False:
        submission_state = 'NONE'
    else:
        submission_state = 'UNKNOWN'
    if submission_state == 'EXISTING' and not version:
        warnings.append('EXISTING_SUBMISSION_VERSION_UNKNOWN')
    if payload.get('submission_version') is not None and version is None:
        warnings.append('SUBMISSION_VERSION_UNVERIFIED')
        submission_state = 'UNKNOWN'

    requires_confirmation = _boolean(payload.get('requires_official_confirmation'))
    if requires_confirmation is None:
        requires_confirmation = False
        if 'OFFICIAL_CONFIRMATION_REQUIRED' not in warnings:
            warnings.append('OFFICIAL_CONFIRMATION_UNKNOWN')
    if 'OFFICIAL_CONFIRMATION_REQUIRED' in warnings:
        requires_confirmation = True

    return {
        'assignment_id': assignment_id,
        'source_record_id': _text(payload.get('source_record_id'), 180) or assignment_id,
        'course_id': _text(payload.get('course_id'), 180),
        'course_name': _text(payload.get('course_name'), 240),
        'title': _text(payload.get('title'), 240),
        'deadline': _text(payload.get('deadline'), 40),
        'deadline_precision': deadline_precision,
        'late_deadline': _text(payload.get('late_deadline'), 40),
        'late_deadline_precision': late_precision,
        'allowed_extensions': extensions,
        'max_bytes': max_bytes or 0,
        'allow_multiple_files': True if allow_multiple is None else allow_multiple,
        'submission_state': submission_state,
        'submission_version': version or 0,
        'submitted_at': submitted_at,
        'requires_official_confirmation': requires_confirmation,
        'completion_type': _text(payload.get('completion_type'), 40),
        'submission_type': _text(payload.get('submission_type'), 40),
        'synthetic': bool(payload.get('synthetic')),
    }, sorted(set(warnings))


def assignment_state(assignment):
    """The business snapshot a plan is bound to; no local path, no note."""
    return {
        'deadline': assignment['deadline'],
        'deadline_precision': assignment['deadline_precision'],
        'late_deadline': assignment['late_deadline'],
        'late_deadline_precision': assignment['late_deadline_precision'],
        'allowed_extensions': list(assignment['allowed_extensions']),
        'max_bytes': assignment['max_bytes'],
        'allow_multiple_files': assignment['allow_multiple_files'],
        'submission_state': assignment['submission_state'],
        'submission_version': assignment['submission_version'],
        'requires_official_confirmation': assignment['requires_official_confirmation'],
        'completion_type': assignment['completion_type'],
        'submission_type': assignment['submission_type'],
    }


def closed(assignment, moment=None):
    """``True`` only when the platform's own rules prove the window is closed."""
    current = moment or datetime.now(source_state.TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=source_state.TZ)
    for key, precision in (('late_deadline', 'late_deadline_precision'),
                           ('deadline', 'deadline_precision')):
        instant, kind = _instant(assignment.get(key))
        if kind == 'UNVERIFIED':
            return True
        if instant is not None:
            # A late deadline supersedes the regular deadline when both exist.
            return current > instant
    return False


def replacement_allowed(assignment, policy, expected_version):
    """Return ``(allowed, code)``; an existing or unknown submission is never overwritten by default."""
    if policy not in REPLACEMENT_POLICIES:
        return False, 'SUBMISSION_INVALID_ARGUMENTS'
    state = assignment['submission_state']
    if state == 'NONE':
        return True, None
    if state == 'UNKNOWN':
        return False, 'SUBMISSION_REPLACEMENT_UNVERIFIED'
    if policy != 'REPLACE_EXISTING':
        return False, 'SUBMISSION_EXISTING_SUBMISSION'
    version = assignment['submission_version']
    if not version or not expected_version:
        return False, 'SUBMISSION_REPLACEMENT_UNVERIFIED'
    if version != expected_version:
        return False, 'SUBMISSION_REPLACEMENT_UNVERIFIED'
    return True, None


def submission_uid(assignment_id, version):
    suffix = str(version) if isinstance(version, int) and version > 0 else 'u'
    return f'{SOURCE_ID}:SUBMISSION:{assignment_id}:{suffix}'


def submission_record(*, assignment_id, state, is_final, version, remote_submission_id,
                      upload_receipt_id, submitted_at, result_observed, warnings=()):
    """Build and check a frozen ``submission-v1`` record.

    The cross-field rules of ``contracts/v0.2/sources/data-models/submission.schema.json``
    are enforced here so an invalid record cannot be emitted even when
    ``jsonschema`` is not installed at runtime.
    """
    if state not in REMOTE_STATES:
        raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'STATE')
    if not _ASSIGNMENT_ID.fullmatch(assignment_id or ''):
        raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'ASSIGNMENT')
    uid = submission_uid(assignment_id, version)
    record = {
        'submission_uid': uid,
        'assignment_uid': f'{ASSIGNMENT_SOURCE_ID}:ASSIGNMENT:{assignment_id}',
        'source_id': SOURCE_ID,
        'state': state,
        'is_final': bool(is_final),
        'version': version if isinstance(version, int) and version >= 1 else None,
        'remote_submission_id': _text(remote_submission_id, 240) or None,
        'upload_receipt_id': _text(upload_receipt_id, 240) or None,
        'submitted_at': source_state.normalize_timestamp(submitted_at, None),
        'result_observed': bool(result_observed),
        'automatic_replay_allowed': False,
        'source_evidence_id': SOURCE_ID,
        'warnings': sorted({str(item)[:200] for item in warnings if item}),
        'synthetic': True,
    }
    if not _UID.fullmatch(record['submission_uid']) or not _ASSIGNMENT_UID.fullmatch(record['assignment_uid']):
        raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'UID')
    if record['state'] == 'SUBMITTED':
        if record['is_final'] is not True or record['result_observed'] is not True:
            raise SubmissionError('SUBMISSION_RESULT_UNVERIFIED', 'SUBMITTED')
        if not record['remote_submission_id']:
            raise SubmissionError('SUBMISSION_RESULT_UNVERIFIED', 'RECEIPT')
    if record['state'] == 'UPLOADED_STAGING' and not record['upload_receipt_id']:
        raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'UPLOAD_RECEIPT')
    return record


def observed_submission(payload, assignment_id):
    """Turn a platform observation into ``(submission-v1 record, warnings)``.

    A payload that claims a final submission without the frozen evidence is
    downgraded to ``UNKNOWN`` instead of being published as ``SUBMITTED``.
    """
    warnings = []
    if not isinstance(payload, dict):
        raise SubmissionError('SUBMISSION_SOURCE_UNAVAILABLE', 'OBSERVATION')
    reported = _text(payload.get('assignment_id'), 180)
    if reported and reported != assignment_id:
        return None, ['OBSERVED_ASSIGNMENT_MISMATCH']
    state = payload.get('state')
    if state not in REMOTE_STATES:
        return submission_record(
            assignment_id=assignment_id, state='UNKNOWN', is_final=False, version=None,
            remote_submission_id=None, upload_receipt_id=None, submitted_at=None,
            result_observed=False, warnings=['OBSERVED_STATE_UNKNOWN']), ['OBSERVED_STATE_UNKNOWN']
    is_final = payload.get('is_final') is True
    result_observed = payload.get('result_observed') is True
    version = _integer(payload.get('version'), 1, 100000)
    remote_id = _text(payload.get('remote_submission_id'), 240) or None
    receipt_id = _text(payload.get('upload_receipt_id'), 240) or None
    submitted_at = _text(payload.get('submitted_at'), 40) or None
    if state == 'SUBMITTED':
        missing = []
        if not is_final:
            missing.append('IS_FINAL_NOT_OBSERVED')
        if not result_observed:
            missing.append('RESULT_NOT_OBSERVED')
        if not remote_id:
            missing.append('REMOTE_SUBMISSION_ID_MISSING')
        if not submitted_at and not remote_id:
            missing.append('RECEIPT_MISSING')
        if missing:
            warnings.extend(missing)
            return submission_record(
                assignment_id=assignment_id, state='UNKNOWN', is_final=False, version=version,
                remote_submission_id=None, upload_receipt_id=receipt_id, submitted_at=None,
                result_observed=False, warnings=missing), sorted(set(warnings))
    if state == 'UPLOADED_STAGING' and not receipt_id:
        warnings.append('UPLOAD_RECEIPT_MISSING')
        state = 'UNKNOWN'
    if state == 'SUBMITTED' and not version:
        warnings.append('VERSION_NOT_PROVIDED')
    return submission_record(
        assignment_id=assignment_id, state=state, is_final=is_final, version=version,
        remote_submission_id=remote_id, upload_receipt_id=receipt_id,
        submitted_at=submitted_at, result_observed=result_observed,
        warnings=warnings), sorted(set(warnings))


def verify_submission(record, observed_files, assignment_id, attachments, expected_version):
    """Compare an observed record with the frozen plan.

    ``content_verified`` is only true when the platform returned a hash for
    every file; names alone never prove identical content.
    """
    rows = observed_files if isinstance(observed_files, list) else []
    planned_names = [item['name'] for item in attachments]
    observed_names = [_text(item.get('name'), 240) for item in rows if isinstance(item, dict)]
    identity = bool(rows) and observed_names == planned_names
    sizes = all(isinstance(item.get('size'), int) and item['size'] >= 0 for item in rows) if rows else False
    if identity and sizes:
        identity = all(item.get('size') == planned['size']
                       for item, planned in zip(rows, attachments))
    content = identity and bool(rows) and all(
        isinstance(item.get('sha256'), str) and item.get('sha256') == planned['sha256']
        for item, planned in zip(rows, attachments))
    version = expected_version if expected_version else None
    observed_version = record.get('version')
    if version:
        # A replacement is verified when the platform reports a new version,
        # not the one the plan set out to replace.
        version_verified = isinstance(observed_version, int) and observed_version >= 1 \
            and observed_version != version
    else:
        version_verified = isinstance(observed_version, int) and observed_version >= 1
    receipt = bool(record.get('submitted_at') or record.get('remote_submission_id'))
    submitted = record.get('state') == 'SUBMITTED' and record.get('is_final') is True
    limitations = []
    if not identity:
        limitations.append('FILE_LIST_NOT_VERIFIED')
    if not content:
        limitations.append('CONTENT_HASH_NOT_AVAILABLE' if identity else 'CONTENT_NOT_VERIFIED')
    if not version_verified:
        limitations.append('VERSION_NOT_PROVIDED' if not observed_version else 'VERSION_NOT_VERIFIED')
    if not receipt:
        limitations.append('RECEIPT_NOT_PROVIDED')
    if record.get('warnings'):
        limitations.extend(record['warnings'])
    verified = bool(submitted and identity and receipt)
    return {
        'state': record.get('state'),
        'is_final': record.get('is_final') is True,
        'identity_verified': identity,
        'content_verified': bool(content),
        'version_verified': bool(version_verified),
        'receipt_verified': receipt,
        'verified': verified,
        'verification_level': 'REMOTE_RECORD_VERIFIED' if verified else 'LOCAL_EXECUTION_STATE',
        'limitations': sorted(set(limitations)),
        'assignment_uid': record.get('assignment_uid'),
        'synthetic': record.get('synthetic') is True,
        'assignment_id': assignment_id,
    }


__all__ = [
    'ASSIGNMENT_SOURCE_ID', 'REMOTE_STATES', 'REPLACEMENT_POLICIES', 'SOURCE_ID',
    'STAGES', 'SUBMISSION_STATES', 'VERSION', 'assignment_state', 'closed',
    'normalize_assignment', 'now_iso', 'observed_submission', 'replacement_allowed',
    'submission_record', 'submission_uid', 'verify_submission',
]
