"""Request identity, cancellation and cleanup for the public facade (6.1).

One call owns one request id from queue to transport to media staging and back.
The MCP transport can cancel a queued or running request; the registry records
which of the two it was, because "never started" and "may have been sent" are
different facts.  Cancelling never replays a request and never undoes a campus
write.

Cleanup hooks are registered by the code that created a temporary resource
(media staging, a polling turn) and run exactly once, whether the call finished
or was cancelled.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading
import time
from typing import Callable

from .errors import FacadeError

QUEUED = 'QUEUED'
RUNNING = 'RUNNING'
CANCELLED = 'CANCELLED'
CANCEL_REQUESTED = 'CANCELLATION_REQUESTED'
FINISHED = 'FINISHED'
FAILED = 'FAILED'
NOT_FOUND = 'NOT_FOUND'
TERMINAL = (CANCELLED, FINISHED, FAILED)

_CURRENT: ContextVar['Request | None'] = ContextVar('openthu_request', default=None)


@dataclass
class Request:
    """One in-flight local request and everything bound to it."""

    request_id: str
    event: threading.Event = field(default_factory=threading.Event)
    state: str = QUEUED
    started_at: float = 0.0
    finished_at: float | None = None
    reason: str | None = None
    cleanup: list[Callable[[], None]] = field(default_factory=list)

    def add_cleanup(self, action: Callable[[], None]) -> None:
        if callable(action):
            self.cleanup.append(action)

    def public(self) -> dict:
        return {'request_id': self.request_id, 'state': self.state,
                'cancelled': self.state in (CANCELLED, CANCEL_REQUESTED),
                'queued': self.state == QUEUED, 'reason': self.reason,
                'started': bool(self.started_at), 'finished': self.state in TERMINAL}


class RequestRegistry:
    """Bounded registry; a finished request is retained briefly for late reads."""

    def __init__(self, *, max_active: int = 64, retain: int = 32,
                 clock: Callable[[], float] = time.monotonic):
        self.max_active = max(1, int(max_active))
        self.retain = max(1, int(retain))
        self.clock = clock
        self._lock = threading.RLock()
        self._requests: dict[str, Request] = {}
        self._order: list[str] = []

    # -- lifecycle ---------------------------------------------------------
    def begin(self, request_id: str) -> Request:
        text = _identifier(request_id)
        with self._lock:
            active = sum(1 for item in self._requests.values() if item.state not in TERMINAL)
            if active >= self.max_active:
                raise FacadeError('FACADE_REQUEST_LIMIT')
            request = Request(request_id=text, started_at=0.0)
            # Reusing an id is refused rather than silently joining another call.
            if text in self._requests and self._requests[text].state not in TERMINAL:
                raise FacadeError('FACADE_REQUEST_LIMIT', detail='DUPLICATE_REQUEST_ID')
            self._requests[text] = request
            self._order.append(text)
            self._prune()
            return request

    def start(self, request: Request) -> Request:
        with self._lock:
            if request.state == QUEUED:
                request.state = RUNNING
                request.started_at = self.clock()
            return request

    def finish(self, request: Request, *, state: str = FINISHED,
               reason: str | None = None) -> Request:
        with self._lock:
            if request.state in TERMINAL:
                return request
            request.state = state
            request.reason = reason or request.reason
            request.finished_at = self.clock()
            hooks, request.cleanup = request.cleanup, []
        _run(hooks)
        return request

    # -- cancellation ------------------------------------------------------
    def cancel(self, request_id: str, reason: str | None = None) -> dict:
        text = _identifier(request_id)
        with self._lock:
            request = self._requests.get(text)
            if request is None:
                return {'request_id': text, 'cancelled': False, 'state': NOT_FOUND,
                        'note': '没有这个本地请求；取消不会创建新请求。'}
            if request.state in (CANCELLED, CANCEL_REQUESTED):
                return {**request.public(), 'cancelled': True}
            if request.state in (FINISHED, FAILED):
                return {**request.public(), 'cancelled': False,
                        'note': '请求已结束，取消失效；不会重放。'}
            queued = request.state == QUEUED
            request.state = CANCELLED if queued else CANCEL_REQUESTED
            request.reason = reason or 'CALLER_CANCELLED'
            request.event.set()
            hooks = [] if not queued else request.cleanup
            if queued:
                request.cleanup = []
                request.finished_at = self.clock()
        # A queued request never reached the transport; a running one keeps its
        # cleanup until the handler returns, so staging is not deleted while it
        # is still being read.
        _run(hooks)
        return {**request.public(), 'cancelled': True,
                'note': '只取消本地排队或执行中的请求，不撤回校园业务写入。',
                'sent_before_cancel': not queued}

    def state(self, request_id: str) -> str:
        text = _identifier(request_id)
        with self._lock:
            request = self._requests.get(text)
            return request.state if request is not None else NOT_FOUND

    def active(self) -> list[Request]:
        with self._lock:
            return [item for item in self._requests.values() if item.state not in TERMINAL]

    # -- internals ---------------------------------------------------------
    def _prune(self) -> None:
        if len(self._order) <= self.retain:
            return
        for key in list(self._order):
            if len(self._order) <= self.retain:
                break
            item = self._requests.get(key)
            if item is None or item.state in TERMINAL:
                self._order.remove(key)
                self._requests.pop(key, None)


def _identifier(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise FacadeError('FACADE_RESULT_INVALID', detail='REQUEST_ID')
    text = str(value)
    if not text or len(text) > 128:
        raise FacadeError('FACADE_RESULT_INVALID', detail='REQUEST_ID')
    return text


def _run(hooks: list[Callable[[], None]]) -> None:
    for action in hooks:
        try:
            action()
        except Exception:
            # Cleanup is best effort; a failed cleanup never becomes a business
            # success or hides the original result.
            continue


@contextmanager
def scope(request: Request | None):
    """Bind one request to the current thread for the duration of the call."""
    token = _CURRENT.set(request)
    try:
        yield request
    finally:
        _CURRENT.reset(token)


def current() -> Request | None:
    return _CURRENT.get()


def current_id() -> str | None:
    request = _CURRENT.get()
    return request.request_id if request is not None else None
