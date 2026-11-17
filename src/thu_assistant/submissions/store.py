"""Protected local stores for submission plans.

Two small records are kept beside the action journal:

* a **file set** binds the local paths to the hashes frozen in the plan, so the
  execute step (possibly in another process after local confirmation) can
  re-read the same bytes;
* a **submission journal** records the pipeline stage, per-file receipts and the
  last observed remote state for one operation.

Both use the same protection codec as ``ActionStore`` (the 2.2 envelope, or
Windows DPAPI through the compatibility wrapper).  The journal is separate from
the action record on purpose: the action engine owns authorization and its
revision-checked state machine, while this module owns upload/draft/submit
evidence and must be able to write it during dispatch and reconciliation
without changing the engine's revision.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid

from learn_assistant.auth import _dpapi_protect, _dpapi_unprotect

from ..catalog import state_root
from .errors import SubmissionError
from .models import STAGES, now_iso

FILE_HEADER = b'THU-SUBMISSION-FILES-1\0'
JOURNAL_HEADER = b'THU-SUBMISSION-STATE-1\0'
_RECORD_ID = re.compile(r'[a-f0-9]{32}')
_OPERATION = re.compile(r'[a-f0-9]{32}')


def _fsync_directory(directory):
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


class _ProtectedStore:
    header = b''
    id_pattern = _RECORD_ID

    def __init__(self, root, protect=_dpapi_protect, unprotect=_dpapi_unprotect):
        self.root = Path(root)
        self.protect = protect
        self.unprotect = unprotect

    def _path(self, record_id):
        if not isinstance(record_id, str) or not self.id_pattern.fullmatch(record_id):
            raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'RECORD_ID')
        return self.root / (record_id + '.dpapi')

    def _write(self, record_id, payload):
        target = self._path(record_id)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_UNAVAILABLE', 'MKDIR') from None
        try:
            blob = self.protect(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8'))
        except Exception:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_UNAVAILABLE', 'ENCRYPT') from None
        if not blob:
            # A configured secure key must fail closed; the test codec is never
            # a production fallback.
            raise SubmissionError('SUBMISSION_LOCAL_STATE_UNAVAILABLE', 'PROTECT')
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(self.header + blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            if os.name != 'nt':
                os.chmod(target, 0o600)
            _fsync_directory(self.root)
        except OSError:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_UNAVAILABLE', 'WRITE') from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _read(self, record_id):
        path = self._path(record_id)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_UNAVAILABLE', 'READ') from None
        if not blob.startswith(self.header):
            raise SubmissionError('SUBMISSION_LOCAL_STATE_CORRUPT', 'HEADER')
        try:
            raw = self.unprotect(blob[len(self.header):])
        except Exception:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_CORRUPT', 'DECRYPT') from None
        if raw is None:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_CORRUPT', 'DECRYPT')
        try:
            payload = json.loads(raw)
        except Exception:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_CORRUPT', 'JSON') from None
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_CORRUPT', 'SHAPE')
        return payload


class FileSetStore(_ProtectedStore):
    """Local paths and hashes for one prepared plan."""

    header = FILE_HEADER

    def __init__(self, root=None, protect=_dpapi_protect, unprotect=_dpapi_unprotect):
        super().__init__(root or state_root() / 'submissions-v1' / 'files', protect, unprotect)

    def put(self, *, operation_digest, account, assignment_id, files):
        file_set_id = uuid.uuid4().hex
        payload = {
            'version': 1,
            'file_set_id': file_set_id,
            'operation_digest': operation_digest,
            'account': account,
            'assignment_id': assignment_id,
            'created_at': now_iso(),
            'files': [{key: item[key] for key in ('path', 'name', 'size', 'sha256', 'extension')}
                      for item in files],
        }
        self._write(file_set_id, payload)
        self.prune()
        return file_set_id

    def get(self, file_set_id):
        payload = self._read(file_set_id)
        if payload is None:
            raise SubmissionError('SUBMISSION_PLAN_MISMATCH', 'FILE_SET_MISSING')
        files = payload.get('files')
        if not isinstance(files, list) or not files:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_CORRUPT', 'FILES')
        for item in files:
            if (not isinstance(item, dict)
                    or not isinstance(item.get('path'), str) or not item['path']
                    or not isinstance(item.get('name'), str) or not item['name']
                    or not isinstance(item.get('size'), int) or item['size'] < 0
                    or not isinstance(item.get('sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])):
                raise SubmissionError('SUBMISSION_LOCAL_STATE_CORRUPT', 'FILE_ROW')
        if payload.get('file_set_id') != file_set_id:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_CORRUPT', 'ID')
        return payload

    def prune(self, keep=200):
        """Best-effort bound on retained file sets; oldest first, never raises."""
        try:
            entries = sorted((path for path in self.root.glob('*.dpapi')),
                             key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            return
        for path in entries[keep:]:
            try:
                path.unlink()
            except OSError:
                pass


class SubmissionJournal(_ProtectedStore):
    """Pipeline stage, receipts and the last observation for one operation."""

    header = JOURNAL_HEADER
    id_pattern = _OPERATION

    def __init__(self, root=None, protect=_dpapi_protect, unprotect=_dpapi_unprotect):
        super().__init__(root or state_root() / 'submissions-v1' / 'plans', protect, unprotect)

    def put(self, operation_id, payload):
        record = dict(payload)
        record['version'] = 1
        record['operation_id'] = operation_id
        self._write(operation_id, record)
        return record

    def get(self, operation_id):
        payload = self._read(operation_id)
        if payload is None:
            return None
        if payload.get('operation_id') != operation_id:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_CORRUPT', 'ID')
        if payload.get('stage') not in STAGES:
            raise SubmissionError('SUBMISSION_LOCAL_STATE_CORRUPT', 'STAGE')
        return payload

    def merge(self, operation_id, change):
        record = self.get(operation_id) or {'version': 1, 'operation_id': operation_id,
                                            'stage': 'PREPARED_LOCAL', 'history': []}
        updated = dict(record)
        change(updated)
        return self.put(operation_id, updated)


__all__ = ['FileSetStore', 'SubmissionJournal']
