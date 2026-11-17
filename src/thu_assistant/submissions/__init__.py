"""Assignment upload and formal submission boundary (module 5.3).

The start conclusion for this module is ``BLOCKED_SOURCE_EVIDENCE``: the
upload, draft-save, final-submit and result-query contracts are unverified, so
no production transport exists and no real endpoint is guessed.  What is
implemented is the boundary that must exist before any such adapter can be
trusted:

* a local plan that freezes the assignment, the file identities and the
  replacement policy, with the files re-hashed after preview;
* an ordered, single-use request sequence (upload per file, draft, final
  submit) driven by the 4.4 action engine, with a durable side-effect ledger;
* separate upload / draft / final / verified / unknown stages, and a
  ``submission-v1`` record that is only emitted from an actual observation;
* a read-only reconciliation path that never replays an uncertain write.

``Submissions(transport=...)`` refuses anything that does not declare itself
synthetic until the source contract is verified.
"""
from __future__ import annotations

from . import errors, manifest, models, store, transport
from .adapter import SubmissionAdapter, enabled
from .client import BLOCKED_SCOPE, SIDE_EFFECTS, Submissions, capability_report
from .contracts import OPERATION, SERVICE, VERSION, contract, contracts, register
from .errors import CODES, SubmissionError, classification_map, classify, error_object
from .manifest import MAX_FILES, describe_file, plan_files, recheck_files
from .models import (
    ASSIGNMENT_SOURCE_ID, REMOTE_STATES, REPLACEMENT_POLICIES, SOURCE_ID, STAGES,
    assignment_state, closed, normalize_assignment, observed_submission,
    replacement_allowed, submission_record, verify_submission,
)
from .store import FileSetStore, SubmissionJournal
from .synthetic import SyntheticSubmissionTransport
from .transport import BlockedSubmissionTransport


__all__ = [
    'ASSIGNMENT_SOURCE_ID', 'BLOCKED_SCOPE', 'BlockedSubmissionTransport', 'CODES',
    'FileSetStore', 'MAX_FILES', 'OPERATION', 'REMOTE_STATES', 'REPLACEMENT_POLICIES',
    'SERVICE', 'SIDE_EFFECTS', 'SOURCE_ID', 'STAGES', 'SubmissionAdapter', 'SubmissionError',
    'SubmissionJournal', 'Submissions', 'SyntheticSubmissionTransport', 'VERSION',
    'assignment_state', 'capability_report', 'classification_map', 'classify', 'closed',
    'contract', 'contracts', 'describe_file', 'enabled', 'errors', 'error_object',
    'manifest', 'models', 'normalize_assignment', 'observed_submission', 'plan_files',
    'recheck_files', 'register', 'replacement_allowed', 'store', 'submission_record',
    'transport', 'verify_submission',
]
