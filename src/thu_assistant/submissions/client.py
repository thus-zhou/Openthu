"""Business entry points for 5.3.

``Submissions.prepare`` reads the assignment, validates the local files,
freezes the business digest and asks the action engine for a plan.  Nothing is
uploaded.  ``execute`` runs the engine's authorized, single-use request
sequence; ``result`` only reads.

Public MCP/CLI registration is 6.1's job.  This class is the boundary 6.1 and the
local board (6.2) consume.
"""
from __future__ import annotations

import time

from .. import actions
from ..action_store import ActionStore
from ..bridge import CampusError
from .adapter import SubmissionAdapter, enabled
from .contracts import OPERATION, SERVICE, VERSION, register
from .errors import SubmissionError
from .manifest import MAX_FILES, plan_files
from .models import SOURCE_ID, assignment_state, now_iso, replacement_allowed, window_state
from .store import FileSetStore, SubmissionJournal
from .transport import BlockedSubmissionTransport

BLOCKED_SCOPE = ['production upload', 'production draft save', 'production final submit']
# Stated in every plan so a preview never implies that only the last button writes.
SIDE_EFFECTS = ['UPLOAD_CREATES_REMOTE_STAGING', 'DRAFT_MAY_BE_REMEMBERED_BY_THE_PLATFORM',
                'FINAL_SUBMIT_IS_NOT_REVERSIBLE_LOCALLY']
FILESET_KEEP = 200


def capability_report():
    """Capability state for facades and documentation, without a transport."""
    return {
        'service_id': SERVICE,
        'operation': OPERATION,
        'source_id': SOURCE_ID,
        'adapter_version': VERSION,
        'start_conclusion': 'BLOCKED_SOURCE_EVIDENCE',
        'implementation_state': 'BLOCKED',
        'runtime_availability': 'UNAVAILABLE',
        'evidence_level': 'NONE',
        'blocked_scope': list(BLOCKED_SCOPE),
        'production_transport': 'NOT_IMPLEMENTED',
        'rehearsal': 'SYNTHETIC_TRANSPORT',
        'write_switch': 'THU_LEARN_SUBMIT',
        'enabled': enabled(),
        'write_switch_default': 'OFF',
        'max_files': MAX_FILES,
        'request_sequence': 'ORDERED_UPLOADS_THEN_DRAFT_THEN_FINAL_SUBMIT',
        'final_submit': 'REQUIRES_SPECIFIC_HUMAN_AUTHORIZATION',
        'side_effects': list(SIDE_EFFECTS),
        'submit_supported': False,
    }


class Submissions:
    """Prepare, execute and read one assignment submission plan."""

    def __init__(self, *, transport=None, store=None, file_store=None, journal=None,
                 account_provider=None, clock=time.time):
        self.transport = transport if transport is not None else BlockedSubmissionTransport()
        self.store = store or ActionStore()
        self.files = file_store or FileSetStore()
        self.journal = journal or SubmissionJournal()
        self.account_provider = account_provider or actions.identity
        self.clock = clock
        self.adapter = SubmissionAdapter(self.transport, self.files, self.journal, clock)
        register()
        self.engine = actions.ActionEngine(
            store=self.store, account_provider=self.account_provider,
            adapters={(SERVICE, OPERATION): self.adapter}, clock=clock)

    # -- capability -------------------------------------------------------
    def describe(self):
        return capability_report()

    # -- plan -------------------------------------------------------------
    def prepare(self, *, assignment_id, file_paths, note='', replacement_policy='NO_OVERWRITE',
                expected_version=0):
        self._require_synthetic('PREPARE')
        if not isinstance(assignment_id, str) or not assignment_id.strip():
            raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'ASSIGNMENT')
        if not isinstance(note, str) or len(note) > 4000:
            raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'NOTE')
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) \
                or expected_version < 0:
            raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'VERSION')
        account = self.account_provider()
        if not isinstance(account, str) or not account:
            raise SubmissionError('SUBMISSION_INVALID_ARGUMENTS', 'ACCOUNT')
        assignment = self.adapter.read_assignment(assignment_id.strip())
        # The deadline is reported first: "unreadable" and "passed" are
        # different diagnoses, and neither is a rule gap in another field.
        window, code = window_state(assignment)
        if window != 'OPEN':
            raise SubmissionError(code or 'SUBMISSION_DEADLINE_UNVERIFIED', 'DEADLINE_' + window)
        if assignment['rules_state'] != 'COMPLETE':
            # A rule the reader did not supply is not a permission to upload.
            raise SubmissionError('SUBMISSION_RULE_UNVERIFIED',
                                  'MISSING_' + '_'.join(assignment['missing_rules']).upper())
        allowed, code = replacement_allowed(assignment, replacement_policy, expected_version)
        if not allowed:
            raise SubmissionError(code)
        files, attachments, warnings = plan_files(file_paths, assignment)
        content_digest = actions.digest(attachments)
        file_set_id = self.files.put(operation_digest=content_digest, account=account,
                                     assignment_id=assignment['assignment_id'], files=files)
        business = {
            'assignment_id': assignment['assignment_id'],
            'course_id': assignment['course_id'],
            'source_record_id': assignment['source_record_id'],
            'source_evidence_id': SOURCE_ID,
            'file_set_id': file_set_id,
            'manifest_sha256': content_digest,
            'attachments': attachments,
            'note': note,
            'replacement_policy': replacement_policy,
            'assignment_state': assignment_state(assignment),
        }
        prepare_warnings = sorted(set(warnings) | set(assignment.get('warnings') or []))
        try:
            plan = self.engine.prepare(SERVICE, OPERATION, business)
        except Exception:
            # A rejected plan must not leave a local record holding the user's
            # file paths and hashes.
            self.files.delete(file_set_id)
            raise
        plan_id = plan['plan_id']
        self.journal.put(plan_id, {
            'stage': 'PREPARED_LOCAL', 'account': account,
            'assignment_id': assignment['assignment_id'],
            'replacement_policy': replacement_policy, 'file_set_id': file_set_id,
            'files': [{'index': index, 'name': item['name'], 'size': item['size'],
                       'sha256': item['sha256'], 'status': 'PENDING', 'receipt_id': None}
                      for index, item in enumerate(files)],
            'prepare_warnings': prepare_warnings,
            'warnings': prepare_warnings,
            'synthetic': True,
            'stage_history': [{'stage': 'PREPARED_LOCAL', 'at': now_iso()}],
        })
        self._prune_file_sets()
        return self._project(plan, self.store.read(plan_id))

    def execute(self, plan_id, authorization):
        self._require_synthetic('EXECUTE')
        self._submission_record(plan_id)
        result = self.engine.execute(plan_id, authorization)
        self._sync_terminal(plan_id, result)
        return self._project(result, self._submission_record(plan_id))

    def result(self, plan_id):
        self._submission_record(plan_id)
        result = self.engine.result(plan_id)
        self._sync_terminal(plan_id, result)
        return self._project(result, self._submission_record(plan_id))

    def cancel(self, plan_id):
        """Local cancellation only; it never withdraws a remote submission."""
        record = self._submission_record(plan_id)
        journal = self.journal.get(plan_id) or {}
        return {'operation_id': plan_id, 'cancelled': False,
                'cancel_reason': 'LOCAL_CANCEL_NO_REMOTE_EFFECT',
                'engine_state': record['state'],
                'remote_changes': 'NONE',
                'stage': journal.get('stage'),
                'next_actions': ['DELETE_THE_LOCAL_PLAN_OR_IGNORE_IT',
                                 'CHECK_THE_OFFICIAL_PAGE_BEFORE_RETRYING'],
                'note': '本地取消不撤回也不重交任何正式作业。'}

    # -- helpers ----------------------------------------------------------
    def _require_synthetic(self, phase):
        if getattr(self.transport, 'synthetic', False) is not True:
            # There is no verified production contract, so only a transport that
            # declares itself synthetic may run.  Enabling a real one is a
            # deliberate, recorded decision after source evidence exists.
            raise SubmissionError('SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED', phase)

    def _submission_record(self, plan_id):
        """Read one plan with the same account binding the engine enforces."""
        record = self.store.read(plan_id)
        if (record.get('service_id') != SERVICE or record.get('operation') != OPERATION
                or record.get('adapter_version') != VERSION):
            raise SubmissionError('SUBMISSION_PLAN_MISMATCH', 'PLAN')
        if record.get('account') != self.account_provider():
            raise CampusError('ACTION_ACCOUNT_MISMATCH')
        return record

    def _prune_file_sets(self):
        """Bound retained file sets without deleting one a live plan references."""
        if self.files.count() <= FILESET_KEEP:
            return
        live = self.journal.live_file_sets()
        self.files.prune(keep=FILESET_KEEP, keep_ids=live)

    def _sync_terminal(self, plan_id, result):
        """Reflect an engine-level terminal failure in the module journal.

        Preflight cannot write the action journal (the engine claims the plan
        with a revision read before it), so it records its specific codes in the
        module journal; this keeps the most specific code visible instead of
        replacing it with the engine's generic reason.
        """
        journal = self.journal.get(plan_id)
        if journal is None or result.get('status') != 'FAILED':
            return
        if journal.get('stage') in ('FAILED', 'VERIFIED'):
            return
        specific = journal.get('preflight_errors') or []

        def change(state):
            state['stage'] = 'FAILED'
            state['engine_reason'] = result.get('reason')
            state['last_error'] = specific[0] if specific else result.get('reason')
            steps = state.setdefault('stage_history', [])
            steps.append({'stage': 'FAILED', 'at': now_iso()})
            state['stage_history'] = steps[-20:]
        self.journal.merge(plan_id, change)

    def _project(self, result, record):
        plan_id = result.get('plan_id') or result.get('operation_id')
        journal = self.journal.get(plan_id) or {}
        business = record.get('business') or {}
        files = [
            {'name': row.get('name'), 'size': row.get('size'), 'sha256': row.get('sha256'),
             'status': row.get('status'), 'receipt_present': bool(row.get('receipt_id'))}
            for row in journal.get('files') or []
        ]
        observed = journal.get('observed')
        return {
            **result,
            'stage': journal.get('stage'),
            'plan': {
                'assignment_id': business.get('assignment_id'),
                'course_id': business.get('course_id'),
                'source_record_id': business.get('source_record_id'),
                'attachments': business.get('attachments') or [],
                'replacement_policy': business.get('replacement_policy'),
                'assignment_state': business.get('assignment_state'),
                'note_present': bool(business.get('note')),
            },
            'files': files,
            'submission': observed,
            'verification': journal.get('verification'),
            'warnings': journal.get('warnings') or [],
            'prepare_warnings': journal.get('prepare_warnings') or [],
            'preflight_errors': journal.get('preflight_errors') or [],
            'pre_submit': journal.get('pre_submit'),
            'last_error': journal.get('last_error'),
            'engine_reason': journal.get('engine_reason'),
            'stage_history': journal.get('stage_history') or [],
            'side_effects': list(SIDE_EFFECTS),
            'synthetic': journal.get('synthetic') is True,
            'production_enabled': False,
            'submit_supported': False,
        }


__all__ = ['BLOCKED_SCOPE', 'SIDE_EFFECTS', 'Submissions', 'capability_report']
