"""The consumer-side ``business-facade-v1`` protocol and its fail-closed stub.

Module 6.1 owns the real facade.  This module must not import it: the local API
only calls the frozen method shapes, so 6.2 can be developed and tested against
an explicit fixture and reconnected to the real facade at batch integration.

Every method returns a ``result-envelope-v1`` mapping for reads.  ``describe``
returns the facade's operation/action description.  A method that cannot read
never returns an empty success: it returns ``UNKNOWN`` with a classified error.
"""
from __future__ import annotations

from functools import lru_cache
import inspect
import threading
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .. import source_state
from . import contract


class CancelToken:
    """Cooperative cancellation for one local request."""

    __slots__ = ('_event',)

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()


@runtime_checkable
class BusinessFacade(Protocol):
    """The only surface the local read API consumes.

    Adapters must:

    * accept ``cancel`` and stop a blocking read within ``ui_wait_seconds``;
      a non-cooperative adapter cannot be killed in-process and is documented
      as a 6.1 integration requirement;
    * return a ``result-envelope-v1`` whose ``source.service_id`` and
      ``source.operation`` equal the requested ones (``todos`` uses
      ``agenda``/``todos``, ``calendar`` uses ``agenda``/``calendar`` and
      ``resources`` uses ``resources``/``resources``), so a mis-routed adapter
      is rejected instead of being shown on the wrong card.
    """

    def health(self, *, service_id: str | None = None,
               cancel: CancelToken | None = None) -> Mapping[str, Any]:
        ...

    def services(self, *, query: str | None = None, category: str | None = None,
                 cancel: CancelToken | None = None) -> Mapping[str, Any]:
        ...

    def query(self, service_id: str, operation: str, parameters: Mapping[str, Any],
              *, cursor: str | None = None, page_size: int = 50,
              cancel: CancelToken | None = None) -> Mapping[str, Any]:
        ...

    def describe(self, service_id: str,
                 cancel: CancelToken | None = None) -> Mapping[str, Any]:
        ...

    def todos(self, *, start_date: str | None = None, end_date: str | None = None,
              cursor: str | None = None, page_size: int = 50,
              cancel: CancelToken | None = None) -> Mapping[str, Any]:
        ...

    def calendar(self, *, start_date: str | None = None, end_date: str | None = None,
                 cursor: str | None = None, page_size: int = 50,
                 cancel: CancelToken | None = None) -> Mapping[str, Any]:
        ...

    def resources(self, *, query: str | None = None, course_id: str | None = None,
                  cursor: str | None = None, page_size: int = 50,
                  cancel: CancelToken | None = None) -> Mapping[str, Any]:
        ...


def _blank(service_id: str, operation: str, *, code: str = 'ADAPTER_REQUIRED',
           warning: str = 'FACADE_NOT_CONNECTED') -> dict[str, Any]:
    checked = source_state.now_iso()
    return {
        'status': 'UNKNOWN',
        'source': {'service_id': service_id, 'operation': operation,
                   'adapter_version': contract.CONTRACT_VERSION,
                   'url': None},
        'checked_at': checked,
        'verification_level': 'NONE',
        'source_complete': False,
        'snapshot_complete': True,
        'complete': False,
        'next_cursor': None,
        'warnings': [warning],
        'missing_pages': [],
        'implementation_state': 'BLOCKED',
        'runtime_availability': 'UNAVAILABLE',
        'evidence_level': 'NONE',
        'items': [],
        'error': source_state.public_error(code, category='NOT_IMPLEMENTED'),
    }


class UnavailableFacade:
    """Production stub used until 6.1 connects the real facade.

    The server therefore starts without a campus account and reports the
    capability gap instead of an empty dashboard or a fabricated result.
    """

    VERSION = 'local-api-facade-unavailable-v1'

    def __init__(self, *, reason: str = 'FACADE_NOT_CONNECTED') -> None:
        self.reason = reason

    def _gap(self, service_id: str, operation: str) -> dict[str, Any]:
        value = _blank(service_id, operation, warning=self.reason)
        value['source']['adapter_version'] = contract.CONTRACT_VERSION
        return value

    def health(self, *, service_id: str | None = None,
               cancel: CancelToken | None = None) -> Mapping[str, Any]:
        checked = source_state.now_iso()
        return {
            'status': 'UNKNOWN',
            'checked_at': checked,
            'services': [{
                'service_id': service_id or 'all',
                'status': 'UNKNOWN',
                'checked_at': checked,
                'complete': False,
                'implementation_state': 'BLOCKED',
                'runtime_availability': 'UNAVAILABLE',
                'evidence_level': 'NONE',
                'warnings': [self.reason],
            }],
            'warnings': [self.reason],
        }

    def services(self, *, query: str | None = None, category: str | None = None,
                 cancel: CancelToken | None = None) -> Mapping[str, Any]:
        return self._gap('local', 'services')

    def query(self, service_id: str, operation: str, parameters: Mapping[str, Any],
              *, cursor: str | None = None, page_size: int = 50,
              cancel: CancelToken | None = None) -> Mapping[str, Any]:
        return self._gap(service_id, operation)

    def describe(self, service_id: str,
                 cancel: CancelToken | None = None) -> Mapping[str, Any]:
        return {'service_id': service_id, 'operations': [], 'actions': [],
                'registry_version': contract.FACADE_CONTRACT_VERSION,
                'warnings': [self.reason],
                'implementation_state': 'BLOCKED',
                'runtime_availability': 'UNAVAILABLE'}

    def todos(self, *, start_date: str | None = None, end_date: str | None = None,
              cursor: str | None = None, page_size: int = 50,
              cancel: CancelToken | None = None) -> Mapping[str, Any]:
        return self._gap('agenda', 'todos')

    def calendar(self, *, start_date: str | None = None, end_date: str | None = None,
                 cursor: str | None = None, page_size: int = 50,
                 cancel: CancelToken | None = None) -> Mapping[str, Any]:
        return self._gap('agenda', 'calendar')

    def resources(self, *, query: str | None = None, course_id: str | None = None,
                 cursor: str | None = None, page_size: int = 50,
                  cancel: CancelToken | None = None) -> Mapping[str, Any]:
        return self._gap('resources', 'resources')


@lru_cache(maxsize=256)
def _accepts_cancel(method: Any) -> bool:
    """Whether a facade method declares ``cancel`` or ``**kwargs``.

    Detecting this by calling and retrying on a ``TypeError`` whose text contains
    ``cancel`` invokes a method twice when its own body raises such an error.
    """
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    if 'cancel' in parameters:
        return True
    return any(item.kind is inspect.Parameter.VAR_KEYWORD
               for item in parameters.values())


def call_with_token(method, *args: Any, cancel: CancelToken | None = None,
                    **kwargs: Any) -> Any:
    """Call a facade method once, forwarding the token only when declared."""
    if cancel is not None and _accepts_cancel(method):
        return method(*args, cancel=cancel, **kwargs)
    return method(*args, **kwargs)


def normalise_service_list(value: Any) -> list[dict[str, Any]]:
    """Keep only display-safe catalog rows."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    rows = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        kept = {key: row.get(key) for key in ('id', 'name', 'category', 'description')
                if isinstance(row.get(key), str)}
        if kept.get('id'):
            rows.append(kept)
    return rows
