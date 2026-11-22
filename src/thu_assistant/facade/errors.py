"""Facade-local errors that still cross the frozen error boundary.

``FacadeError`` is a ``CampusError`` so the existing MCP/CLI mapping keeps its
code.  The difference is the optional ``capability`` block: when a registered
capability is missing, blocked or disabled, the public result names that
capability instead of only a bare code.  No exception text, host name, path or
token is copied into the block.
"""
from __future__ import annotations

from ..bridge import CampusError

# Every code below is registered in contracts/v0.2/error-classification.json and
# mirrored in source_state.ERROR_CODES by the module tests.
CODES = (
    'FACADE_CAPABILITY_UNAVAILABLE',
    'FACADE_WRITE_NOT_REGISTERED',
    'FACADE_QUERY_CANNOT_PROXY',
    'FACADE_OPERATION_NOT_REGISTERED',
    'FACADE_SERVICE_UNKNOWN',
    'FACADE_REQUEST_LIMIT',
    'FACADE_RESULT_INVALID',
    'LOCAL_DASHBOARD_UNAVAILABLE',
    'RESOURCE_PROVIDER_UNAVAILABLE',
)

class FacadeError(CampusError):
    """A public facade refusal with an optional sanitised capability block."""

    def __init__(self, code: str, *, capability=None, detail: str | None = None):
        super().__init__(code)
        self.capability = dict(capability) if isinstance(capability, dict) else {}
        self.detail = detail if isinstance(detail, str) and len(detail) <= 120 else None

    def public(self) -> dict:
        """The public error object: code plus capability evidence, no text."""
        from .. import source_state
        value = source_state.public_error(self.code)
        if self.capability:
            value['capability'] = dict(self.capability)
        return value


def gap(code: str, capability) -> FacadeError:
    """Build the refusal for a registered capability that cannot run now."""
    block = capability.public() if hasattr(capability, 'public') else dict(capability or {})
    return FacadeError(code, capability=block)
