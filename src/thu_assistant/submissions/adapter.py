"""Adapter: local plan -> ordered uploads -> draft -> final submit -> result check.

Every write goes through the action engine's one-use ``RequestGate``; the
dispatch records intent per step in the ``SideEffectLedger`` before the
transport is called.  There is no retry path here: a lost or failed response
leaves the engine in ``UNKNOWN`` and only reads can continue.

Preflight and reconcile are intentionally free of writes to the action journal.
The engine claims the plan with a revision captured before ``preflight`` and
re-reads the record with a revision captured before ``reconcile``; a write to
the same journal from either hook would turn into a silent no-op.  Module-owned
evidence therefore lives in the separate submission journal.
"""
from __future__ import annotations

from copy import deepcopy
import os
import time

from ..actions import NotDispatched
from ..bridge import CampusError
from .errors import SubmissionError
from .manifest import read_verified, recheck_files
from .models import (
    VERSION, assignment_state, closed, normalize_assignment, now_iso, observed_submission,
    replacement_allowed, verify_submission,
)
from .transport import references, request_specs


def enabled():
    """Module-owned write switch; action_enabled() has no learn branch yet."""
    return os.environ.get('THU_LEARN_SUBMIT', '1') != '0'


def _error_code(error):
    if isinstance(error, SubmissionError):
        return error.code
    code = getattr(error, 'code', None)
    return code if isinstance(code, str) else 'SUBMISSION_WRITE_UNKNOWN'


class SubmissionAdapter:
    version = VERSION
    accepts_ledger = True

    def __init__(self, transport, files, journal, clock=time.time):
        self.transport = transport
        self.files = files
        self.journal = journal
        self.clock = clock

    # -- engine hooks -----------------------------------------------------
    def preview(self, business):
        """Validate the frozen business object; no I/O, no upload."""
        file_set = self.files.get(business['file_set_id'])
        if file_set['assignment_id'] != business['assignment_id']:
            raise SubmissionError('SUBMISSION_PLAN_MISMATCH', 'ASSIGNMENT')
        stored = [(item['name'], item['size'], item['sha256']) for item in file_set['files']]
        frozen = [(item['name'], item['size'], item['sha256']) for item in business['attachments']]
        if stored != frozen:
            raise SubmissionError('SUBMISSION_PLAN_MISMATCH', 'ATTACHMENTS')
        return business

    def preflight(self, record):
        if not enabled():
            raise SubmissionError('SUBMISSION_WRITES_DISABLED', 'SWITCH')
        business = record['business']
        file_set = self.files.get(business['file_set_id'])
        if file_set['assignment_id'] != business['assignment_id']:
            raise SubmissionError('SUBMISSION_PLAN_MISMATCH', 'ASSIGNMENT')
        if file_set.get('account') != record.get('account'):
            # File sets are account-scoped; a plan may not upload files that
            # were prepared under another account.
            raise SubmissionError('SUBMISSION_PLAN_MISMATCH', 'ACCOUNT')
        stored = [(item['name'], item['size'], item['sha256']) for item in file_set['files']]
        frozen = [(item['name'], item['size'], item['sha256']) for item in business['attachments']]
        if stored != frozen:
            raise SubmissionError('SUBMISSION_PLAN_MISMATCH', 'ATTACHMENTS')

        assignment = self.read_assignment(business['assignment_id'])
        checks = {}
        errors = []
        current, file_errors = recheck_files(file_set['files'])
        checks['fees'] = not file_errors
        errors.extend(file_errors)
        checks['rules'] = assignment_state(assignment) == business['assignment_state']
        if not checks['rules']:
            errors.append('SUBMISSION_PLAN_MISMATCH')
        checks['availability'] = not closed(assignment)
        if not checks['availability']:
            errors.append('SUBMISSION_ASSIGNMENT_CLOSED')
        allowed, code = replacement_allowed(
            assignment, business['replacement_policy'],
            business['assignment_state']['submission_version'])
        checks['eligibility'] = allowed
        if code:
            errors.append(code)
        fresh = {**business, 'attachments': current}
        requests = []
        handoff = None
        if all(checks.values()):
            if assignment['requires_official_confirmation']:
                # The platform wants a declaration or official verification we
                # cannot perform: hand the step over instead of sending a write.
                handoff = 'OFFICIAL_CONFIRMATION_REQUIRED'
            else:
                refs = references(record['operation_id'], business['attachments'])
                requests = request_specs(business, refs, self.transport.requires_draft)
        return {'business': fresh, 'context': record['context'], 'checks': checks,
                'check_errors': sorted(set(errors)), 'requests': requests,
                'official_handoff': handoff}

    def dispatch(self, record, gate, ledger):
        business = record['business']
        operation_id = record['operation_id']
        file_set = self.files.get(business['file_set_id'])
        refs = references(operation_id, business['attachments'])
        self._begin(record, file_set, refs)
        try:
            for index, item in enumerate(file_set['files']):
                step = f'upload-{index}'
                ledger.declare(step)
                try:
                    payload = read_verified(item)
                except SubmissionError as error:
                    self._fail(operation_id, index, error.code)
                    if gate.sent == 0:
                        raise NotDispatched() from None
                    raise error
                spec = self._upload_spec(business, refs, index)
                if not gate.consume(spec.url, spec.method, spec.body, spec.frame):
                    # The frozen sequence and the adapter disagree: nothing may
                    # be sent under a request the engine did not authorize.
                    self._fail(operation_id, index, 'SUBMISSION_PLAN_MISMATCH')
                    if gate.sent == 0:
                        raise NotDispatched() from None
                    raise CampusError('REQUEST_SEQUENCE_REQUIRED')
                ledger.sent(step)
                try:
                    receipt = self.transport.send(spec, payload)
                except Exception as error:
                    ledger.unknown(step)
                    self._file(operation_id, index, 'UNKNOWN')
                    self._unknown(operation_id, _error_code(error))
                    raise
                self._file(operation_id, index, 'ACCEPTED', receipt)
            self._transition(operation_id, 'UPLOADED_STAGING')
            if self.transport.requires_draft:
                self._send_step(operation_id, gate, ledger, 'draft',
                                self._draft_body(business, refs))
                self._transition(operation_id, 'DRAFT_SAVED', draft_ref=refs['draft'])
            receipt = self._send_step(operation_id, gate, ledger, 'submit',
                                      self._submit_body(business, refs))
            accepted = isinstance(receipt, dict) and receipt.get('accepted') is not False
            if not accepted:
                self._unknown(operation_id, 'SUBMISSION_PLATFORM_REJECTED')
            self._transition(operation_id, 'SUBMIT_SENT_UNVERIFIED',
                             submission_ref=refs['submission'], submit_accepted=accepted)
            return receipt
        except NotDispatched:
            self._transition(operation_id, 'FAILED', last_error='REQUEST_NOT_SENT')
            raise

    def reconcile(self, record):
        business = record['business']
        try:
            observed = self.transport.read_submission(business['assignment_id'])
        except SubmissionError:
            raise
        except Exception:
            raise SubmissionError('SUBMISSION_SOURCE_UNAVAILABLE', 'READ') from None
        if not isinstance(observed, dict) or observed.get('complete') is not True:
            self._observe(record['operation_id'], None, None, ['OBSERVATION_INCOMPLETE'])
            return {'records': [], 'complete': False}
        payload = observed.get('submission')
        if not isinstance(payload, dict):
            self._observe(record['operation_id'], None, None, [])
            return {'records': [], 'complete': True}
        record_observed, warnings = observed_submission(payload, business['assignment_id'])
        if record_observed is None:
            self._observe(record['operation_id'], None, None, warnings)
            return {'records': [], 'complete': True}
        verification = verify_submission(
            record_observed, payload.get('files'), business['assignment_id'],
            business['attachments'], business['assignment_state']['submission_version'])
        self._observe(record['operation_id'], record_observed, verification, warnings)
        base = {'business': deepcopy(business), 'account': record['account'],
                'service_id': record['service_id'], 'operation': record['operation'],
                'platform': 'learn'}
        state = record_observed['state']
        if verification['verified']:
            return {'records': [{**base, 'record_id': record_observed['remote_submission_id'],
                                 'state': 'SUCCEEDED'}], 'complete': True}
        if state in ('FAILED', 'SUPERSEDED'):
            return {'records': [{**base, 'record_id': record_observed['remote_submission_id']
                                 or record_observed['submission_uid'],
                                 'state': 'FAILED'}], 'complete': True}
        return {'records': [], 'complete': True}

    # -- helpers ----------------------------------------------------------
    def read_assignment(self, assignment_id):
        try:
            payload = self.transport.read_assignment(assignment_id)
        except SubmissionError:
            raise
        except Exception:
            raise SubmissionError('SUBMISSION_SOURCE_UNAVAILABLE', 'READ') from None
        assignment, warnings = normalize_assignment(payload, assignment_id)
        if assignment is None:
            raise SubmissionError('SUBMISSION_ASSIGNMENT_NOT_FOUND')
        assignment['warnings'] = warnings
        return assignment

    def _upload_spec(self, business, refs, index):
        return request_specs(business, refs, self.transport.requires_draft)[index]

    def _draft_body(self, business, refs):
        index = len(business['attachments'])
        return request_specs(business, refs, True)[index]

    def _submit_body(self, business, refs):
        index = len(business['attachments']) + (1 if self.transport.requires_draft else 0)
        return request_specs(business, refs, self.transport.requires_draft)[index]

    def _send_step(self, operation_id, gate, ledger, step, spec):
        ledger.declare(step)
        if not gate.consume(spec.url, spec.method, spec.body, spec.frame):
            self._unknown(operation_id, 'SUBMISSION_PLAN_MISMATCH')
            raise CampusError('REQUEST_SEQUENCE_REQUIRED')
        ledger.sent(step)
        try:
            return self.transport.send(spec, None)
        except Exception as error:
            ledger.unknown(step)
            self._unknown(operation_id, _error_code(error))
            raise

    # -- journal ----------------------------------------------------------
    def _begin(self, record, file_set, refs):
        business = record['business']
        files = [{'index': index, 'name': item['name'], 'size': item['size'],
                  'sha256': item['sha256'], 'status': 'PENDING', 'receipt_id': None}
                 for index, item in enumerate(file_set['files'])]
        self.journal.merge(record['operation_id'], lambda state: state.update(
            stage='UPLOADING', account=record['account'], digest=record['digest'],
            assignment_id=business['assignment_id'],
            replacement_policy=business['replacement_policy'],
            file_set_id=business['file_set_id'], refs=refs, files=files,
            dispatch_at=now_iso(), synthetic=True))

    def _transition(self, operation_id, stage, **extra):
        def change(state):
            state['stage'] = stage
            state.update(extra)
            steps = state.setdefault('stage_history', [])
            steps.append({'stage': stage, 'at': now_iso()})
            state['stage_history'] = steps[-20:]
        self.journal.merge(operation_id, change)

    def _file(self, operation_id, index, status, receipt=None):
        def change(state):
            rows = state.setdefault('files', [])
            for row in rows:
                if row.get('index') == index:
                    row['status'] = status
                    if isinstance(receipt, dict) and receipt.get('receipt_id'):
                        row['receipt_id'] = str(receipt['receipt_id'])[:240]
                    return
        self.journal.merge(operation_id, change)

    def _fail(self, operation_id, index, code):
        self._file(operation_id, index, 'NOT_SENT')
        self._transition(operation_id, 'FAILED', last_error=code)

    def _unknown(self, operation_id, code):
        self._transition(operation_id, 'UNKNOWN', last_error=code)

    def _observe(self, operation_id, observed, verification, warnings):
        def change(state):
            stage = state.get('stage', 'UNKNOWN')
            if verification and verification.get('verified'):
                stage = 'VERIFIED'
            elif observed and observed.get('state') in ('FAILED', 'SUPERSEDED'):
                stage = 'FAILED'
            elif observed and observed.get('state') == 'DRAFT_SAVED':
                stage = 'DRAFT_SAVED'
            elif observed and observed.get('state') == 'UPLOADED_STAGING':
                stage = 'UPLOADED_STAGING'
            elif not verification:
                stage = 'UNKNOWN'
            state['stage'] = stage
            state['observed'] = observed
            state['verification'] = verification
            state['observed_at'] = now_iso()
            state['warnings'] = sorted({str(item)[:200] for item in warnings if item})
            steps = state.setdefault('stage_history', [])
            steps.append({'stage': stage, 'at': now_iso()})
            state['stage_history'] = steps[-20:]
        self.journal.merge(operation_id, change)


__all__ = ['SubmissionAdapter', 'enabled']
