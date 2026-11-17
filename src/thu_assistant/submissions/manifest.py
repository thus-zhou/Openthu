"""Local attachment validation and content identity.

The preparation step only reads, hashes and checks; it never uploads.  The same
functions run again in preflight and once more immediately before each upload,
so a file edited between preview and dispatch invalidates the plan instead of
being submitted under an old hash.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import os

from .errors import SubmissionError

# The action engine's one-use gate allows at most ten requests.  A plan uses one
# upload per file plus one draft save and one final submit, so eight files is
# the largest plan that can still be expressed as one exact request sequence.
MAX_FILES = 8
MAX_TOTAL_BYTES = 256 * 1024 * 1024
_CHUNK = 1024 * 1024


def sha256_file(path, limit=MAX_TOTAL_BYTES + 1):
    digest = hashlib.sha256()
    read = 0
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            read += len(chunk)
            if read > limit:
                raise SubmissionError('SUBMISSION_FILE_UNSAFE', 'TOO_LARGE')
            digest.update(chunk)
    return digest.hexdigest(), read


def _resolved(path):
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        raise SubmissionError('SUBMISSION_FILE_NOT_FOUND', 'PATH') from None


def describe_file(path):
    """Return the local identity of one file; raises a classified error."""
    candidate = Path(path).expanduser()
    try:
        if candidate.is_symlink():
            # A link could be re-pointed between preview and dispatch, so the
            # plan refuses it instead of binding the target's hash.
            raise SubmissionError('SUBMISSION_FILE_UNSAFE', 'SYMLINK')
    except OSError:
        raise SubmissionError('SUBMISSION_FILE_NOT_FOUND', 'PATH') from None
    resolved = _resolved(candidate)
    if not resolved.exists():
        raise SubmissionError('SUBMISSION_FILE_NOT_FOUND', 'MISSING')
    if not resolved.is_file():
        raise SubmissionError('SUBMISSION_FILE_UNSAFE', 'NOT_REGULAR')
    try:
        size = resolved.stat().st_size
    except OSError:
        raise SubmissionError('SUBMISSION_FILE_NOT_FOUND', 'STAT') from None
    if size == 0:
        raise SubmissionError('SUBMISSION_FILE_UNSAFE', 'EMPTY')
    try:
        digest, read = sha256_file(resolved)
    except OSError:
        raise SubmissionError('SUBMISSION_FILE_NOT_FOUND', 'READ') from None
    if read != size:
        raise SubmissionError('SUBMISSION_FILE_CHANGED', 'SIZE')
    return {
        'path': str(resolved),
        'name': resolved.name,
        'size': size,
        'sha256': digest,
        'extension': resolved.suffix.casefold(),
    }


def plan_files(paths, assignment):
    """Validate the candidate file set against the assignment rules.

    Returns ``(files, attachments, warnings)``.  ``files`` keeps the local path
    for later re-reads and never leaves the protected local store; only
    ``attachments`` (name, size, sha256) enters the action journal and public
    output.
    """
    if not isinstance(paths, (list, tuple)) or not paths:
        raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'FILES')
    if len(paths) > MAX_FILES:
        raise SubmissionError('SUBMISSION_LIMIT_EXCEEDED', 'COUNT')
    if assignment.get('allowed_extensions_state') != 'DECLARED':
        raise SubmissionError('SUBMISSION_RULE_UNVERIFIED', 'ALLOWED_EXTENSIONS')
    if assignment.get('max_bytes_state') != 'DECLARED':
        raise SubmissionError('SUBMISSION_RULE_UNVERIFIED', 'MAX_BYTES')
    if assignment.get('multiple_files_state') == 'UNVERIFIED':
        raise SubmissionError('SUBMISSION_RULE_UNVERIFIED', 'ALLOW_MULTIPLE_FILES')
    if len(paths) > 1 and assignment['allow_multiple_files'] is not True:
        raise SubmissionError('SUBMISSION_FILE_UNSAFE', 'MULTIPLE')
    allowed = {item.casefold() for item in assignment['allowed_extensions']}
    max_bytes = assignment['max_bytes'] or None
    warnings = []
    files = []
    names = set()
    total = 0
    for raw in paths:
        if not isinstance(raw, (str, os.PathLike)):
            raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'PATH_TYPE')
        item = describe_file(raw)
        key = item['path'].casefold()
        if any(existing['path'].casefold() == key for existing in files):
            raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'DUPLICATE')
        if item['name'].casefold() in names:
            # Two files with one remote name would be ambiguous in the result
            # check, so the plan refuses them instead of merging identities.
            raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'DUPLICATE_NAME')
        if allowed and item['extension'] not in allowed:
            raise SubmissionError('SUBMISSION_FILE_UNSAFE', 'EXTENSION')
        if max_bytes is not None and item['size'] > max_bytes:
            raise SubmissionError('SUBMISSION_FILE_UNSAFE', 'SIZE_LIMIT')
        total += item['size']
        if total > MAX_TOTAL_BYTES:
            raise SubmissionError('SUBMISSION_LIMIT_EXCEEDED', 'TOTAL')
        names.add(item['name'].casefold())
        files.append(item)
    if not assignment['allowed_extensions']:
        warnings.append('ALLOWED_EXTENSIONS_EMPTY')
    if not max_bytes:
        warnings.append('SIZE_LIMIT_NOT_PROVIDED')
    attachments = [{'name': item['name'], 'size': item['size'], 'sha256': item['sha256']}
                   for item in files]
    return files, attachments, sorted(set(warnings))


def recheck_files(files):
    """Re-read every planned file and compare name, size and hash exactly.

    Returns ``(attachments, errors)``; ``errors`` holds classified codes so the
    preflight can invalidate the plan without raising a transport error.
    """
    attachments = []
    errors = []
    for item in files:
        try:
            current = describe_file(item.get('path'))
        except SubmissionError as error:
            errors.append(error.code)
            attachments.append({'name': item.get('name'), 'size': item.get('size'),
                                'sha256': None})
            continue
        if (current['name'] != item.get('name') or current['size'] != item.get('size')
                or current['sha256'] != item.get('sha256')):
            errors.append('SUBMISSION_FILE_CHANGED')
        attachments.append({'name': current['name'], 'size': current['size'],
                            'sha256': current['sha256']})
    return attachments, sorted(set(errors))


def read_verified(item):
    """Read the bytes that exactly match the frozen identity, or refuse."""
    try:
        payload = Path(item['path']).read_bytes()
    except OSError:
        raise SubmissionError('SUBMISSION_FILE_NOT_FOUND', 'READ') from None
    if len(payload) != item['size'] or hashlib.sha256(payload).hexdigest() != item['sha256']:
        raise SubmissionError('SUBMISSION_FILE_CHANGED', 'CONTENT')
    return payload


__all__ = [
    'MAX_FILES', 'MAX_TOTAL_BYTES', 'describe_file', 'plan_files', 'read_verified',
    'recheck_files', 'sha256_file',
]
