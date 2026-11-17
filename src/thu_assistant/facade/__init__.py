"""The single public business facade (module 6.1).

Every public entry point consumes ``BusinessFacade`` instead of re-implementing
availability, account scope, envelope or error rules.  The frozen method set,
capability registration and request/cancel binding live in this package.
"""
from .core import LOCAL_METHODS, METHODS, VERSION, BusinessFacade
from .errors import CODES, FacadeError, gap
from .registry import (
    AUTHORIZATIONS,
    CAMPUS_WRITE,
    KINDS,
    LOCAL_FILE_WRITE,
    LOCAL_PLAN_WRITE,
    MODEL_REQUEST,
    NO_EFFECT,
    STATES,
    Providers,
    Registration,
    registrations,
)
from .requests import (CANCELLED, CANCEL_REQUESTED, FINISHED, QUEUED, RUNNING,
                       Request, RequestRegistry, current, current_id)

__all__ = [
    'AUTHORIZATIONS', 'BusinessFacade', 'CANCEL_REQUESTED', 'CANCELLED', 'CODES',
    'CAMPUS_WRITE', 'FacadeError', 'FINISHED', 'KINDS', 'LOCAL_FILE_WRITE',
    'LOCAL_METHODS', 'LOCAL_PLAN_WRITE', 'METHODS', 'MODEL_REQUEST', 'NO_EFFECT',
    'Providers', 'QUEUED', 'RUNNING', 'Registration', 'Request', 'RequestRegistry',
    'STATES', 'VERSION', 'current', 'current_id', 'gap', 'registrations',
]
