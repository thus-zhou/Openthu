"""Models for the assignment submission boundary.

Two state axes are deliberately separate:

* ``STAGES`` is the local pipeline (what Openthu itself has done and observed).
* ``REMOTE_STATES`` is the frozen ``submission-v1`` enum, only ever filled in
  from an observation made on the platform.

``submission-v1`` requires ``SUBMITTED`` to carry ``is_final=true``,
``result_observed=true`` and a ``remote_submission_id``; a local "final submit
was sent, the result is not back yet" therefore stays ``SUBMIT_SENT_UNVERIFIED``
on the stage axis and never becomes a ``SUBMITTED`` record.

Assignment rules are read fail-closed: a rule the reader did not supply is
``UNVERIFIED`` and blocks the plan.  The module never turns "the reader omitted
this field" into "the platform allows anything".
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
NON_FINAL_STATES = ('PREPARED_LOCAL', 'UPLOAD_STARTED', 'UPLOADED_STAGING', 'DRAFT_SAVED',
                    'SUPERSEDED', 'FAILED', 'UNKNOWN')
SUBMISSION_STATES = ('NONE', 'EXISTING', 'UNKNOWN')
REPLACEMENT_POLICIES = ('NO_OVERWRITE', 'REPLACE_EXISTING')

# Rules a reader must supply before a plan may be built.  An absent key is not
# the same as a declared value; see ``normalize_assignment``.
REQUIRED_RULES = ('deadline', 'allowed_extensions', 'max_bytes', 'allow_multiple_files',
                  'requires_official_confirmation')

_ASSIGNMENT_ID = re.compile(r'[A-Za-z0-9_.:-]{1,180}')
_UID = re.compile(r'^[a-z][a-z0-9_.-]{1,80}:SUBMISSION:[A-Za-z0-9_.:-]{1,180}$')
_ASSIGNMENT_UID = re.compile(r'^[a-z][a-z0-9_.-]{1,80}:ASSIGNMENT:[A-Za-z0-9_.:-]{1,180}$')
_SOURCE_ID = re.compile(r'^[a-z][a-z0-9_.-]{2,120}$')


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

    ``NONE`` means the reader declared no deadline, ``UNVERIFIED`` means the
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


def _extensions(value):
    """A non-empty list of controlled extensions, or ``None`` (unverified)."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    result = []
    for item in value:
        text = _text(item, 20).casefold()
        if not text:
            return None
        if not text.startswith('.'):
            text = '.' + text
        if not re.fullmatch(r'\.[a-z0-9]{1,12}', text):
            return None
        if text not in result:
            result.append(text)
    return result


def normalize_assignment(payload, assignment_id):
    """Normalise a reader payload without inventing rule values.

    Every rule is reported as a state: a parsed deadline, a bounded size, a
    list of extensions, ``ALLOWED``/``SINGLE`` and ``REQUIRED``/``NOT_REQUIRED``.
    A missing, empty or malformed rule becomes ``UNVERIFIED`` and is listed in
    ``missing_rules``; callers must refuse such a plan.  A malformed value is
    never read as "no submission" or "no deadline".
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
    missing = []

    # deadline: an explicit ``deadline_state='NONE'`` is the only way to declare
    # "no deadline"; an absent or unreadable value stays unverified.
    declared_deadline_state = _text(payload.get('deadline_state'), 20).upper()
    deadline, deadline_precision = _instant(payload.get('deadline'))
    if declared_deadline_state == 'NONE':
        deadline, deadline_precision = None, 'NONE'
    elif deadline_precision == 'UNVERIFIED':
        warnings.append('DEADLINE_UNVERIFIED')
    elif deadline_precision == 'DATE':
        warnings.append('DEADLINE_DATE_PRECISION')
    elif deadline_precision == 'NONE':
        # An absent deadline is not "no deadline": only an explicit
        # ``deadline_state='NONE'`` may declare that.
        deadline_precision = 'UNVERIFIED'
        warnings.append('DEADLINE_NOT_PROVIDED')
    if deadline_precision in ('NONE', 'UNVERIFIED'):
        missing.append('deadline')

    raw_late = payload.get('late_deadline', payload.get('late_submission_deadline'))
    late_deadline, late_precision = _instant(raw_late)
    if _text(payload.get('late_deadline_state'), 20).upper() == 'NONE':
        late_deadline, late_precision = None, 'NONE'
    if late_precision == 'UNVERIFIED':
        warnings.append('LATE_DEADLINE_UNVERIFIED')
    elif late_precision == 'NONE' and 'late_deadline' not in payload and \
            'late_submission_deadline' not in payload:
        # Absence of a late window is conservative: ``window_state`` then uses
        # the regular deadline, which can only close the window earlier.
        warnings.append('LATE_DEADLINE_NOT_PROVIDED')

    extensions = _extensions(payload.get('allowed_extensions'))
    if extensions is None:
        warnings.append('ALLOWED_EXTENSIONS_UNVERIFIED')
        missing.append('allowed_extensions')

    max_bytes = _integer(payload.get('max_bytes'), 1, 1073741824)
    if max_bytes is None:
        warnings.append('MAX_BYTES_UNVERIFIED')
        missing.append('max_bytes')

    allow_multiple = _boolean(payload.get('allow_multiple_files'))
    if allow_multiple is None:
        warnings.append('MULTIPLE_FILES_RULE_UNVERIFIED')
        missing.append('allow_multiple_files')

    requires_confirmation = _boolean(payload.get('requires_official_confirmation'))
    if requires_confirmation is None:
        warnings.append('OFFICIAL_CONFIRMATION_UNKNOWN')
        missing.append('requires_official_confirmation')
    elif requires_confirmation:
        warnings.append('OFFICIAL_CONFIRMATION_REQUIRED')

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

    missing = sorted(set(missing))
    return {
        'assignment_id': assignment_id,
        'source_record_id': _text(payload.get('source_record_id'), 180) or assignment_id,
        'course_id': _text(payload.get('course_id'), 180),
        'course_name': _text(payload.get('course_name'), 240),
        'title': _text(payload.get('title'), 240),
        'deadline': _text(payload.get('deadline'), 40),
        'deadline_precision': deadline_precision,
        'late_deadline': _text(raw_late, 40),
        'late_deadline_precision': late_precision,
        'allowed_extensions': extensions or [],
        'allowed_extensions_state': 'DECLARED' if extensions else 'UNVERIFIED',
        'max_bytes': max_bytes or 0,
        'max_bytes_state': 'DECLARED' if max_bytes else 'UNVERIFIED',
        'allow_multiple_files': bool(allow_multiple) if allow_multiple is not None else False,
        'multiple_files_state': ('UNVERIFIED' if allow_multiple is None else
                                 'ALLOWED' if allow_multiple else 'SINGLE'),
        'submission_state': submission_state,
        'submission_version': version or 0,
        'submitted_at': submitted_at,
        'requires_official_confirmation': requires_confirmation is True,
        'confirmation_state': ('UNVERIFIED' if requires_confirmation is None else
                               'REQUIRED' if requires_confirmation else 'NOT_REQUIRED'),
        'completion_type': _text(payload.get('completion_type'), 40),
        'submission_type': _text(payload.get('submission_type'), 40),
        'rules_state': 'COMPLETE' if not missing else 'PARTIAL',
        'missing_rules': missing,
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
        'allowed_extensions_state': assignment['allowed_extensions_state'],
        'max_bytes': assignment['max_bytes'],
        'max_bytes_state': assignment['max_bytes_state'],
        'allow_multiple_files': assignment['allow_multiple_files'],
        'multiple_files_state': assignment['multiple_files_state'],
        'submission_state': assignment['submission_state'],
        'submission_version': assignment['submission_version'],
        'requires_official_confirmation': assignment['requires_official_confirmation'],
        'confirmation_state': assignment['confirmation_state'],
        'completion_type': assignment['completion_type'],
        'submission_type': assignment['submission_type'],
        'rules_state': assignment['rules_state'],
        'missing_rules': list(assignment['missing_rules']),
    }


def window_state(assignment, moment=None):
    """``(state, code)`` where state is ``OPEN``, ``CLOSED`` or ``UNVERIFIED``.

    An unreadable deadline is not a passed deadline: it is a rule the reader
    must fix, and it blocks the plan with its own code.
    """
    current = moment or datetime.now(source_state.TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=source_state.TZ)
    if assignment.get('deadline_precision') == 'UNVERIFIED' \
            or assignment.get('late_deadline_precision') == 'UNVERIFIED':
        return 'UNVERIFIED', 'SUBMISSION_DEADLINE_UNVERIFIED'
    for key, precision in (('late_deadline', 'late_deadline_precision'),
                           ('deadline', 'deadline_precision')):
        instant, kind = _instant(assignment.get(key))
        if instant is None:
            continue
        if current > instant:
            return 'CLOSED', 'SUBMISSION_ASSIGNMENT_CLOSED'
        return 'OPEN', None
    return 'OPEN', None


def closed(assignment, moment=None):
    """Fail-closed helper: anything that is not provably open counts as closed."""
    return window_state(assignment, moment)[0] != 'OPEN'


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
                      upload_receipt_id, submitted_at, result_observed, warnings=(),
                      synthetic=True, source_evidence_id=SOURCE_ID):
    """Build and check a frozen ``submission-v1`` record.

    The cross-field rules of ``contracts/v0.2/sources/data-models/submission.schema.json``
    are enforced here so an invalid record cannot be emitted even when
    ``jsonschema`` is not installed at runtime.  A non-synthetic record is
    refused: this build has no verified live source, so claiming one would be a
    false evidence statement.
    """
    if state not in REMOTE_STATES:
        raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'STATE')
    if not _ASSIGNMENT_ID.fullmatch(assignment_id or ''):
        raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'ASSIGNMENT')
    if synthetic is not True:
        raise SubmissionError('SUBMISSION_LIVE_EVIDENCE_UNVERIFIED', 'SYNTHETIC')
    if not isinstance(source_evidence_id, str) or not _SOURCE_ID.fullmatch(source_evidence_id):
        raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'SOURCE')
    is_final = bool(is_final)
    # submission.schema.json allOf: SUBMITTED is the only final state and every
    # other state must declare is_final=false.
    if state == 'SUBMITTED' and not is_final:
        raise SubmissionError('SUBMISSION_RESULT_UNVERIFIED', 'IS_FINAL')
    if state != 'SUBMITTED' and is_final:
        raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'IS_FINAL')
    record = {
        'submission_uid': submission_uid(assignment_id, version),
        'assignment_uid': f'{ASSIGNMENT_SOURCE_ID}:ASSIGNMENT:{assignment_id}',
        'source_id': SOURCE_ID,
        'state': state,
        'is_final': is_final,
        'version': version if isinstance(version, int) and version >= 1 else None,
        'remote_submission_id': _text(remote_submission_id, 240) or None,
        'upload_receipt_id': _text(upload_receipt_id, 240) or None,
        'submitted_at': source_state.normalize_timestamp(submitted_at, None),
        'result_observed': bool(result_observed),
        'automatic_replay_allowed': False,
        'source_evidence_id': source_evidence_id,
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


def observed_submission(payload, assignment_id, synthetic=True):
    """Turn a platform observation into ``(submission-v1 record, warnings)``.

    The observation must identify the assignment itself (``assignment_id`` or
    ``assignment_uid``); without that identity no record is built, because a
    file list alone cannot prove which assignment a submission belongs to.  A
    payload that claims a final submission without the frozen evidence, or a
    non-final state that claims ``is_final=true``, is downgraded instead of
    being published as-is.
    """
    warnings = []
    if not isinstance(payload, dict):
        raise SubmissionError('SUBMISSION_SOURCE_UNAVAILABLE', 'OBSERVATION')
    reported_id = _text(payload.get('assignment_id'), 180)
    reported_uid = _text(payload.get('assignment_uid'), 300)
    uid_assignment = ''
    if reported_uid:
        parts = reported_uid.split(':ASSIGNMENT:')
        if len(parts) == 2 and parts[0] == ASSIGNMENT_SOURCE_ID \
                and _ASSIGNMENT_UID.fullmatch(reported_uid):
            uid_assignment = parts[1]
    if not reported_id and not uid_assignment:
        return None, ['ASSIGNMENT_ID_NOT_OBSERVED']
    if reported_id and uid_assignment and reported_id != uid_assignment:
        return None, ['ASSIGNMENT_IDENTITY_MISMATCH']
    if (reported_id or uid_assignment) != assignment_id:
        return None, ['OBSERVED_ASSIGNMENT_MISMATCH']
    payload_synthetic = _boolean(payload.get('synthetic'))
    if payload_synthetic is False and synthetic is True:
        warnings.append('SYNTHETIC_FLAG_MISMATCH')
    source_evidence_id = _text(payload.get('source_evidence_id'), 120) or SOURCE_ID
    if not _SOURCE_ID.fullmatch(source_evidence_id):
        source_evidence_id = SOURCE_ID
        warnings.append('SOURCE_EVIDENCE_ID_INVALID')
    state = payload.get('state')
    if state not in REMOTE_STATES:
        return submission_record(
            assignment_id=assignment_id, state='UNKNOWN', is_final=False, version=None,
            remote_submission_id=None, upload_receipt_id=None, submitted_at=None,
            result_observed=False, warnings=['OBSERVED_STATE_UNKNOWN'],
            synthetic=synthetic, source_evidence_id=source_evidence_id), \
            ['OBSERVED_STATE_UNKNOWN']
    is_final = payload.get('is_final') is True
    result_observed = payload.get('result_observed') is True
    version = _integer(payload.get('version'), 1, 100000)
    remote_id = _text(payload.get('remote_submission_id'), 240) or None
    receipt_id = _text(payload.get('upload_receipt_id'), 240) or None
    submitted_at = _text(payload.get('submitted_at'), 40) or None
    if state != 'SUBMITTED' and is_final:
        # A non-final remote state cannot be a final submission; keep the state
        # and record the contradiction instead of emitting an invalid record.
        warnings.append('IS_FINAL_CONTRADICTED')
        is_final = False
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
                result_observed=False, warnings=missing, synthetic=synthetic,
                source_evidence_id=source_evidence_id), sorted(set(warnings))
    if state == 'UPLOADED_STAGING' and not receipt_id:
        warnings.append('UPLOAD_RECEIPT_MISSING')
        state = 'UNKNOWN'
    if state == 'SUBMITTED' and not version:
        warnings.append('VERSION_NOT_PROVIDED')
    return submission_record(
        assignment_id=assignment_id, state=state, is_final=is_final, version=version,
        remote_submission_id=remote_id, upload_receipt_id=receipt_id,
        submitted_at=submitted_at, result_observed=result_observed,
        warnings=warnings, synthetic=synthetic,
        source_evidence_id=source_evidence_id), sorted(set(warnings))


def verify_submission(record, observed_files, assignment_id, attachments, expected_version,
                      observed_assignment_uid=None):
    """Compare an observed record with the frozen plan.

    ``content_verified`` is only true when the platform returned a hash for
    every file; names alone never prove identical content.  The assignment
    identity is compared from the observation when it carries one, so a record
    built for a different assignment can never be verified here.
    """
    if not isinstance(record, dict):
        return {'state': None, 'is_final': False, 'identity_verified': False,
                'content_verified': False, 'version_verified': False,
                'receipt_verified': False, 'verified': False,
                'verification_level': 'LOCAL_EXECUTION_STATE',
                'limitations': ['SUBMISSION_RECORD_MISSING'], 'assignment_uid': None,
                'synthetic': False, 'assignment_id': assignment_id}
    rows = [item for item in observed_files if isinstance(item, dict)] \
        if isinstance(observed_files, list) else []
    invalid_rows = isinstance(observed_files, list) and len(rows) != len(observed_files)
    planned_uid = f'{ASSIGNMENT_SOURCE_ID}:ASSIGNMENT:{assignment_id}'
    record_uid = record.get('assignment_uid') if isinstance(record, dict) else None
    identity_uid = record_uid == planned_uid
    if observed_assignment_uid and observed_assignment_uid != planned_uid:
        identity_uid = False
    planned_names = [item['name'] for item in attachments]
    observed_names = [_text(item.get('name'), 240) for item in rows]
    identity = identity_uid and bool(rows) and observed_names == planned_names and not invalid_rows
    sizes = all(isinstance(item.get('size'), int) and item['size'] >= 0
                for item in rows) if rows else False
    if identity and sizes:
        identity = all(item.get('size') == planned['size']
                       for item, planned in zip(rows, attachments))
    content = identity and bool(rows) and all(
        isinstance(item.get('sha256'), str) and item.get('sha256') == planned['sha256']
        for item, planned in zip(rows, attachments))
    version = expected_version if expected_version else None
    observed_version = record.get('version')
    if version:
        # A replacement is verified when the platform reports a *newer* version
        # than the one the plan set out to replace; older or equal is refused.
        version_verified = isinstance(observed_version, int) and observed_version > version
    else:
        version_verified = isinstance(observed_version, int) and observed_version >= 1
    receipt = bool(record.get('submitted_at') or record.get('remote_submission_id'))
    submitted = record.get('state') == 'SUBMITTED' and record.get('is_final') is True
    limitations = []
    if not identity_uid:
        limitations.append('ASSIGNMENT_UID_MISMATCH')
    if invalid_rows:
        limitations.append('OBSERVED_FILE_ROW_INVALID')
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
    verified = bool(submitted and identity and receipt and identity_uid)
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
        'assignment_uid': record_uid,
        'synthetic': record.get('synthetic') is True,
        'assignment_id': assignment_id,
    }


__all__ = [
    'ASSIGNMENT_SOURCE_ID', 'NON_FINAL_STATES', 'REMOTE_STATES', 'REPLACEMENT_POLICIES',
    'REQUIRED_RULES', 'SOURCE_ID', 'STAGES', 'SUBMISSION_STATES', 'VERSION',
    'assignment_state', 'closed', 'normalize_assignment', 'now_iso', 'observed_submission',
    'replacement_allowed', 'submission_record', 'submission_uid', 'verify_submission',
    'window_state',
]
