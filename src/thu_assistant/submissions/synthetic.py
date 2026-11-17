"""Synthetic submission transport for engineering rehearsal.

It is explicitly ``synthetic = True`` and never contacts a campus service.  The
remote model is deliberately small: an upload store, an optional draft and an
optional final submission, with switchable failure behaviour so every recovery
path in the module can be exercised offline.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .. import source_state
from .errors import SubmissionError

DEFAULT_ASSIGNMENT = {
    'assignment_id': 'SYNTHETIC-ASSIGNMENT-1',
    'course_id': 'SYNTHETIC-COURSE-1',
    'course_name': '合成课程',
    'title': '合成作业一',
    'deadline': '2099-01-01 23:59',
    'late_deadline': '',
    'allowed_extensions': ['pdf', 'txt', 'md', 'zip'],
    'max_bytes': 1048576,
    'allow_multiple_files': True,
    'submitted': False,
    'submitted_at': '',
    'submission_version': 0,
    'requires_official_confirmation': False,
    'completion_type': 'FILE',
    'submission_type': 'FILE',
}

BEHAVIOURS = (
    'ok',
    'lost_upload',
    'reject_upload',
    'upload_not_accepted',
    'draft_not_accepted',
    'draft_only',
    'submit_lost',
    'submit_unknown',
    'names_only',
    'official_confirmation',
    'existing',
    'existing_changes',
    'closed',
    'superseded',
    'failed',
    'foreign',
)


class SyntheticSubmissionTransport:
    """A declared-synthetic transport; never a production default."""

    synthetic = True

    def __init__(self, assignment=None, behavior='ok', requires_draft=True, fail_at=0):
        if behavior not in BEHAVIOURS:
            raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'BEHAVIOUR')
        self.assignment = dict(DEFAULT_ASSIGNMENT)
        if assignment:
            self.assignment.update(assignment)
        self.behavior = behavior
        self.requires_draft = requires_draft
        self.fail_at = fail_at
        self.uploads = {}
        self.upload_order = []
        self.draft = None
        self.submission = None
        self.counts = {'read_assignment': 0, 'read_submission': 0, 'upload': 0,
                       'draft': 0, 'submit': 0, 'sent': 0}
        self.sent_specs = []
        if behavior in ('existing', 'existing_changes'):
            self.assignment.update(submitted=True, submitted_at='2098-12-01 10:00',
                                   submission_version=3)

    # -- reads -----------------------------------------------------------
    def read_assignment(self, assignment_id):
        self.counts['read_assignment'] += 1
        if assignment_id != self.assignment['assignment_id']:
            return None
        row = deepcopy(self.assignment)
        if self.behavior == 'closed':
            row['deadline'] = '2000-01-01 00:00'
        if self.behavior == 'official_confirmation':
            row['requires_official_confirmation'] = True
        if self.behavior == 'existing_changes' and self._changed:
            row['submission_version'] = 4
            row['submitted_at'] = '2098-12-02 10:00'
        row['synthetic'] = True
        return row

    _changed = False

    def change_existing_version(self):
        self._changed = True

    def read_submission(self, assignment_id):
        self.counts['read_submission'] += 1
        if assignment_id != self.assignment['assignment_id']:
            return {'submission': None, 'complete': True}
        if self.behavior in ('superseded', 'failed', 'foreign'):
            state = {'superseded': 'SUPERSEDED', 'failed': 'FAILED',
                     'foreign': 'SUBMITTED'}[self.behavior]
            return {'complete': True, 'submission': {
                'assignment_id': 'SYNTHETIC-OTHER-ASSIGNMENT' if self.behavior == 'foreign' else assignment_id,
                'state': state, 'is_final': state == 'SUBMITTED',
                'version': 3, 'remote_submission_id': f'SYN-{state}',
                'upload_receipt_id': None, 'submitted_at': '2099-01-02 10:00',
                'result_observed': True, 'files': self._observed_files(),
                'synthetic': True}}
        if self.submission is not None:
            return {'complete': True, 'submission': deepcopy(self.submission)}
        if self.draft is not None:
            return {'complete': True, 'submission': {
                'assignment_id': assignment_id, 'state': 'DRAFT_SAVED', 'is_final': False,
                'version': 0, 'remote_submission_id': None,
                'upload_receipt_id': self.draft['draft_ref'], 'submitted_at': None,
                'result_observed': False, 'files': self._observed_files(), 'synthetic': True}}
        if self.uploads:
            return {'complete': True, 'submission': {
                'assignment_id': assignment_id, 'state': 'UPLOADED_STAGING', 'is_final': False,
                'version': 0, 'remote_submission_id': None,
                'upload_receipt_id': self.upload_order[0], 'submitted_at': None,
                'result_observed': False, 'files': self._observed_files(), 'synthetic': True}}
        return {'submission': None, 'complete': True}

    def _observed_files(self):
        rows = []
        for reference in (self.draft['uploads'] if self.draft else self.upload_order):
            item = self.uploads.get(reference)
            if item is None:
                continue
            row = {'name': item['name']}
            if self.behavior != 'names_only':
                row['size'] = item['size']
                row['sha256'] = item['sha256']
            rows.append(row)
        return rows

    # -- writes ----------------------------------------------------------
    def send(self, spec, payload=None):
        self.counts['sent'] += 1
        self.sent_specs.append({'url': spec.url, 'method': spec.method, 'body': spec.body})
        body = json.loads(spec.body)
        upload_ref = body.get('upload_ref')
        draft_ref = body.get('draft_ref')
        submission_ref = body.get('submission_ref')
        if isinstance(upload_ref, str) and spec.url.endswith('/upload/' + upload_ref):
            return self._upload(body, payload)
        if isinstance(draft_ref, str) and spec.url.endswith('/draft/' + draft_ref):
            return self._save_draft(body)
        if isinstance(submission_ref, str) and spec.url.endswith('/submit/' + submission_ref):
            return self._submit(body)
        raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'REQUEST')

    def _upload(self, body, payload):
        index = self.counts['upload']
        self.counts['upload'] += 1
        if payload is None or hashlib.sha256(payload).hexdigest() != body['sha256']:
            raise SubmissionError('SUBMISSION_FILE_CHANGED', 'PAYLOAD')
        if self.behavior == 'reject_upload' and index == self.fail_at:
            # A definite rejection: nothing was stored.
            raise SubmissionError('SUBMISSION_PLATFORM_REJECTED', 'UPLOAD')
        if self.behavior == 'upload_not_accepted' and index == self.fail_at:
            # HTTP 200 but the platform says it did not store the file.
            return {'accepted': False, 'receipt_id': 'SYN-REJECTED-' + body['upload_ref'][:8]}
        self.uploads[body['upload_ref']] = {'name': body['name'], 'size': body['size'],
                                            'sha256': body['sha256']}
        if body['upload_ref'] not in self.upload_order:
            self.upload_order.append(body['upload_ref'])
        if self.behavior == 'lost_upload' and index == self.fail_at:
            # The server stored the file; the response never came back.
            raise TimeoutError('synthetic-lost-upload-response')
        return {'receipt_id': 'SYN-UPLOAD-' + body['upload_ref'][:8], 'accepted': True}

    def _save_draft(self, body):
        self.counts['draft'] += 1
        for reference in body['uploads']:
            if reference not in self.uploads:
                raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'DRAFT_UPLOADS')
        if self.behavior == 'draft_not_accepted':
            return {'accepted': False, 'receipt_id': 'SYN-DRAFT-REJECTED'}
        self.draft = {'draft_ref': body['draft_ref'], 'uploads': list(body['uploads']),
                      'note': body.get('note', '')}
        return {'receipt_id': 'SYN-DRAFT-' + body['draft_ref'][:8], 'accepted': True}

    def _submit(self, body):
        self.counts['submit'] += 1
        if self.draft is None and self.requires_draft:
            raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'NO_DRAFT')
        if self.behavior == 'draft_only':
            return {'accepted': False, 'reason': 'DRAFT_ONLY'}
        version = (self.assignment.get('submission_version') or 0) + 1
        files = self._observed_files()
        self.submission = {
            'assignment_id': self.assignment['assignment_id'],
            'state': 'SUBMITTED', 'is_final': True, 'version': version,
            'remote_submission_id': 'SYN-SUBMISSION-' + body['submission_ref'][:8],
            'upload_receipt_id': self.draft['draft_ref'] if self.draft else None,
            'submitted_at': source_state.now_iso(),
            'result_observed': True, 'files': files, 'synthetic': True,
        }
        if self.behavior == 'submit_unknown':
            self.submission['result_observed'] = False
        if self.behavior != 'submit_unknown':
            # The platform now reports one more submission version.
            self.assignment.update(submitted=True, submitted_at=self.submission['submitted_at'],
                                   submission_version=version)
        if self.behavior == 'submit_lost':
            raise TimeoutError('synthetic-lost-submit-response')
        return {'receipt_id': self.submission['remote_submission_id'], 'accepted': True}


__all__ = ['BEHAVIOURS', 'DEFAULT_ASSIGNMENT', 'SyntheticSubmissionTransport']
