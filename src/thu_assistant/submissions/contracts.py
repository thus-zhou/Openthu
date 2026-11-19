"""Action contract for a formal assignment submission.

The contract is registered into the frozen action engine registry by
``register()``.  Public MCP/CLI wiring belongs to 6.1; this module only owns the
operation, its input schema and the adapter descriptor.

``enabled`` is ``False`` on purpose: ``actions.action_enabled`` only enables the
library and sports services and fails closed for every other service, so this
module must not inherit the sports switch.  Submission write enablement is owned
here (``THU_LEARN_SUBMIT``) and checked by the adapter before any request is
built.
"""
from __future__ import annotations

from .. import actions
from ..queries import schema
from .manifest import MAX_FILES
from .models import REPLACEMENT_POLICIES, VERSION

SERVICE = 'learn'
OPERATION = 'submit_assignment'

ID = {'type': 'string', 'pattern': r'[A-Za-z0-9_.:-]{1,180}'}
ATTACHMENT = schema({
    'name': {'type': 'string', 'minLength': 1, 'maxLength': 240},
    'size': {'type': 'integer', 'minimum': 1, 'maximum': 1073741824},
    'sha256': {'type': 'string', 'pattern': r'[a-f0-9]{64}'},
}, ('name', 'size', 'sha256'))
ASSIGNMENT_STATE = schema({
    'deadline': {'type': 'string', 'maxLength': 40},
    'deadline_precision': {'type': 'string', 'pattern': 'NONE|DATE|EXACT|UNVERIFIED'},
    'late_deadline': {'type': 'string', 'maxLength': 40},
    'late_deadline_precision': {'type': 'string', 'pattern': 'NONE|DATE|EXACT|UNVERIFIED'},
    'allowed_extensions': {'type': 'array', 'items': {'type': 'string', 'maxLength': 20},
                           'maxItems': 40},
    'allowed_extensions_state': {'type': 'string', 'pattern': 'DECLARED|UNVERIFIED'},
    'max_bytes': {'type': 'integer', 'minimum': 0, 'maximum': 1073741824},
    'max_bytes_state': {'type': 'string', 'pattern': 'DECLARED|UNVERIFIED'},
    'allow_multiple_files': {'type': 'boolean'},
    'multiple_files_state': {'type': 'string', 'pattern': 'ALLOWED|SINGLE|UNVERIFIED'},
    'submission_state': {'type': 'string', 'pattern': 'NONE|EXISTING|UNKNOWN'},
    'submission_version': {'type': 'integer', 'minimum': 0, 'maximum': 100000},
    'requires_official_confirmation': {'type': 'boolean'},
    'confirmation_state': {'type': 'string', 'pattern': 'REQUIRED|NOT_REQUIRED|UNVERIFIED'},
    'completion_type': {'type': 'string', 'maxLength': 40},
    'submission_type': {'type': 'string', 'maxLength': 40},
    'rules_state': {'type': 'string', 'pattern': 'COMPLETE|PARTIAL'},
    'missing_rules': {'type': 'array', 'items': {'type': 'string', 'maxLength': 40},
                      'maxItems': 40},
}, ('deadline', 'deadline_precision', 'late_deadline', 'late_deadline_precision',
    'allowed_extensions', 'allowed_extensions_state', 'max_bytes', 'max_bytes_state',
    'allow_multiple_files', 'multiple_files_state', 'submission_state',
    'submission_version', 'requires_official_confirmation', 'confirmation_state',
    'completion_type', 'submission_type', 'rules_state', 'missing_rules'))

INPUT = {
    'assignment_id': ID,
    'course_id': {'type': 'string', 'maxLength': 180},
    'source_record_id': ID,
    'source_evidence_id': {'type': 'string', 'pattern': r'[a-z][a-z0-9_.-]{2,120}'},
    'file_set_id': {'type': 'string', 'pattern': r'[a-f0-9]{32}'},
    'manifest_sha256': {'type': 'string', 'pattern': r'[a-f0-9]{64}'},
    'attachments': {'type': 'array', 'items': ATTACHMENT, 'minItems': 1, 'maxItems': MAX_FILES},
    'note': {'type': 'string', 'maxLength': 4000},
    'replacement_policy': {'type': 'string',
                           'pattern': '|'.join(REPLACEMENT_POLICIES)},
    'assignment_state': ASSIGNMENT_STATE,
}
REQUIRED = ('assignment_id', 'source_record_id', 'source_evidence_id', 'file_set_id',
            'manifest_sha256', 'attachments', 'replacement_policy', 'assignment_state')


def contract():
    return {
        'version': VERSION,
        'input_schema': schema(INPUT, REQUIRED),
        'schema': schema(INPUT),
        'required': REQUIRED,
        'preview_fields': ['assignment_id', 'course_id', 'attachments', 'replacement_policy'],
        'critical_fields': list(INPUT),
        'prechecks': actions.CHECKS,
        'risk_conditions': [
            'DEADLINE_OR_RULE_CHANGED',
            'EXISTING_SUBMISSION_CONFLICT',
            'ATTACHMENT_CHANGED',
            'OFFICIAL_CONFIRMATION_REQUIRED',
            'RESULT_UNVERIFIED',
            'REQUEST_SEQUENCE_INCOMPLETE',
        ],
        'request_sequence': 'ORDERED_UPLOADS_THEN_DRAFT_THEN_FINAL_SUBMIT',
        'reconciliation': 'OWN_ASSIGNMENT_SUBMISSION_VERSION_AND_FILES',
        'enabled': False,
    }


def contracts():
    return {(SERVICE, OPERATION): contract()}


def register(registry=None):
    """Idempotently add the operation to the action engine registry.

    ``VERIFIED_ADAPTERS`` is extended as well: without that entry the engine
    ignores an ``official_handoff`` and would insist on a request sequence that
    does not exist for a confirmation-only step.
    """
    target = actions.REGISTRY if registry is None else registry
    target[(SERVICE, OPERATION)] = contract()
    actions.VERIFIED_ADAPTERS.add(VERSION)
    return contract()


__all__ = ['ASSIGNMENT_STATE', 'INPUT', 'OPERATION', 'REQUIRED', 'SERVICE', 'VERSION',
           'contract', 'contracts', 'register']
