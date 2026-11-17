"""Submission transport boundary.

No production transport exists in this build: the upload, draft-save,
final-submit and result-query contracts are unverified
(``contracts/v0.2/sources/adapter-readiness.json``, module 5.3).  A transport
must therefore declare ``synthetic = True`` explicitly to be usable at all, and
the request identities carry the reserved ``/synthetic/`` path so no real
endpoint is guessed.

Request bodies use client-generated references only.  The action engine hands
out one exact, ordered, single-use request capability, so a body cannot carry a
server-issued identifier that is still unknown when the plan is frozen.
"""
from __future__ import annotations

import hashlib
import json

from ..actions import RequestSpec
from .errors import SubmissionError

BASE = 'https://learn.tsinghua.edu.cn/synthetic/openthu/submission'


def upload_url(reference):
    return f'{BASE}/upload/{reference}'


def draft_url(reference):
    return f'{BASE}/draft/{reference}'


def submit_url(reference):
    return f'{BASE}/submit/{reference}'


def _body(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def references(operation_id, attachments):
    """Deterministic client references derived from the plan and file hashes."""
    seed = f'{operation_id}|'

    def derive(kind, index=''):
        return hashlib.sha256(f'{seed}{kind}|{index}'.encode('utf-8')).hexdigest()[:32]

    uploads = []
    for index, item in enumerate(attachments):
        raw = f'{seed}upload|{index}|{item["sha256"]}|{item["name"]}'
        uploads.append(hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32])
    draft = derive('draft')
    submission = derive('submit')
    return {'uploads': uploads, 'draft': draft, 'submission': submission}


def request_specs(business, refs, requires_draft=True):
    """The frozen, ordered write sequence for one plan."""
    assignment = business['assignment_id']
    specs = []
    for index, item in enumerate(business['attachments']):
        specs.append(RequestSpec(
            upload_url(refs['uploads'][index]), 'POST',
            _body({'assignment_id': assignment, 'upload_ref': refs['uploads'][index],
                   'name': item['name'], 'size': item['size'], 'sha256': item['sha256']}),
        ))
    if requires_draft:
        specs.append(RequestSpec(
            draft_url(refs['draft']), 'POST',
            _body({'assignment_id': assignment, 'draft_ref': refs['draft'],
                   'uploads': list(refs['uploads']), 'note': business.get('note', '')}),
        ))
    specs.append(RequestSpec(
        submit_url(refs['submission']), 'POST',
        _body({'assignment_id': assignment, 'submission_ref': refs['submission'],
               'draft_ref': refs['draft'] if requires_draft else None,
               'expected_version': business['assignment_state']['submission_version']}),
    ))
    return specs


class BlockedSubmissionTransport:
    """Default transport: refuses every call, including reads."""

    synthetic = False
    requires_draft = True

    def read_assignment(self, assignment_id):
        raise SubmissionError('SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED', 'READ')

    def read_submission(self, assignment_id):
        raise SubmissionError('SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED', 'READ')

    def send(self, spec, payload=None):
        raise SubmissionError('SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED', 'WRITE')


__all__ = [
    'BASE', 'BlockedSubmissionTransport', 'draft_url', 'references', 'request_specs',
    'submit_url', 'upload_url',
]
