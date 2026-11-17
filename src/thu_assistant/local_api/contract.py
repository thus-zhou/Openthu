"""Frozen ``local-read-api-v1`` mirror: routes, limits, statuses and errors.

The JSON contract in ``contracts/v0.2/local-read-api.schema.json`` is the source
of truth.  This module is the runtime copy the server enforces, and
``tests/test_local_api.py`` compares the two so drift fails a test instead of
reaching a browser.

Deliberate, documented extension: the frozen route list declares an opaque
``next_cursor`` response field on ``/todos``, ``/calendar`` and ``/resources``
but omits the matching request field.  Without it those routes could not page at
all, so the server accepts ``cursor`` there.  ``QUERY_EXTENSION`` records the
gap and the delivery record routes it to the contract owner; every other query
parameter is exactly the frozen list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

from .. import source_state

CONTRACT_VERSION = 'local-read-api-v1'
FACADE_CONTRACT_VERSION = 'business-facade-v1'
BASE_PATH = '/api/v1'

DEFAULT_BIND = '127.0.0.1'
ALLOWED_BINDS = ('127.0.0.1', '::1')
HOST_ALLOWLIST = ('127.0.0.1', 'localhost', '[::1]')
METHOD_ALLOWLIST = ('GET', 'HEAD')

# Engineering limits, not school rules.  Kept identical to the frozen contract
# except for the local response/session bounds the contract leaves open.
LIMITS = {
    'request_body_bytes': 65536,
    'page_size_default': 50,
    'page_size_max': 200,
    'ui_wait_seconds': 30.0,
    'refresh_min_seconds': 10.0,
    'cursor_ttl_seconds': 600.0,
    'max_path_characters': 2048,
    'max_query_characters': 8192,
    'max_parameters_characters': 4096,
    'max_response_bytes': 8 * 1024 * 1024,
    'max_page_items': 5000,
    'max_sessions': 16,
    'max_requests_per_window': 120,
    'request_window_seconds': 10.0,
    'bootstrap_ttl_seconds': 300.0,
    'session_ttl_seconds': 14400.0,
    'session_idle_seconds': 1800.0,
}


@dataclass(frozen=True)
class LocalApiLimits:
    """Server-side knobs; defaults come from the frozen contract."""

    request_body_bytes: int = LIMITS['request_body_bytes']
    page_size_default: int = LIMITS['page_size_default']
    page_size_max: int = LIMITS['page_size_max']
    ui_wait_seconds: float = LIMITS['ui_wait_seconds']
    refresh_min_seconds: float = LIMITS['refresh_min_seconds']
    cursor_ttl_seconds: float = LIMITS['cursor_ttl_seconds']
    max_path_characters: int = LIMITS['max_path_characters']
    max_query_characters: int = LIMITS['max_query_characters']
    max_parameters_characters: int = LIMITS['max_parameters_characters']
    max_response_bytes: int = LIMITS['max_response_bytes']
    max_page_items: int = LIMITS['max_page_items']
    max_sessions: int = LIMITS['max_sessions']
    max_requests_per_window: int = LIMITS['max_requests_per_window']
    request_window_seconds: float = LIMITS['request_window_seconds']
    bootstrap_ttl_seconds: float = LIMITS['bootstrap_ttl_seconds']
    session_ttl_seconds: float = LIMITS['session_ttl_seconds']
    session_idle_seconds: float = LIMITS['session_idle_seconds']

    def describe(self) -> dict[str, Any]:
        return {
            'request_body_bytes': self.request_body_bytes,
            'page_size_default': self.page_size_default,
            'page_size_max': self.page_size_max,
            'ui_wait_seconds': self.ui_wait_seconds,
            'refresh_min_seconds': self.refresh_min_seconds,
            'cursor_ttl_seconds': self.cursor_ttl_seconds,
            'max_path_characters': self.max_path_characters,
            'max_query_characters': self.max_query_characters,
            'max_parameters_characters': self.max_parameters_characters,
            'max_response_bytes': self.max_response_bytes,
            'max_page_items': self.max_page_items,
            'max_sessions': self.max_sessions,
            'max_requests_per_window': self.max_requests_per_window,
            'request_window_seconds': self.request_window_seconds,
            'bootstrap_ttl_seconds': self.bootstrap_ttl_seconds,
            'session_ttl_seconds': self.session_ttl_seconds,
            'session_idle_seconds': self.session_idle_seconds,
        }


@dataclass(frozen=True)
class Route:
    name: str
    template: str
    pattern: re.Pattern[str]
    query: tuple[str, ...]
    kind: str  # 'read', 'catalog', 'detail' or 'static'
    facade: str | None = None


SERVICE_ID = r'[A-Za-z0-9_.-]{1,64}'
OPERATION = r'[A-Za-z0-9_.-]{1,120}'

ROUTES: tuple[Route, ...] = (
    Route('health', '/health', re.compile(r'^/api/v1/health$'), (), 'read', 'health'),
    Route('services', '/services', re.compile(r'^/api/v1/services$'),
          ('query', 'category'), 'catalog', 'services'),
    Route('service_detail', '/services/{service_id}',
          re.compile(r'^/api/v1/services/(?P<service_id>' + SERVICE_ID + r')$'),
          (), 'detail', 'describe'),
    Route('query', '/query/{service_id}/{operation}',
          re.compile(r'^/api/v1/query/(?P<service_id>' + SERVICE_ID + r')/'
                     r'(?P<operation>' + OPERATION + r')$'),
          ('parameters', 'cursor', 'page_size'), 'read', 'query'),
    Route('todos', '/todos', re.compile(r'^/api/v1/todos$'),
          ('start_date', 'end_date', 'page_size', 'cursor'), 'read', 'todos'),
    Route('calendar', '/calendar', re.compile(r'^/api/v1/calendar$'),
          ('start_date', 'end_date', 'page_size', 'cursor'), 'read', 'calendar'),
    Route('resources', '/resources', re.compile(r'^/api/v1/resources$'),
          ('query', 'course_id', 'page_size', 'cursor'), 'read', 'resources'),
)

# The only query-parameter extension over the frozen list.  Reported in
# ``describe_api()`` and in the delivery record rather than hidden.
QUERY_EXTENSION = {
    'parameter': 'cursor',
    'routes': ['/todos', '/calendar', '/resources'],
    'reason': 'local-read-api-v1 declares next_cursor on these routes but omits the request field',
    'status': 'PENDING_CONTRACT_OWNER_REVISION',
}

# Route names that must never exist in the read-only API.
FORBIDDEN_ROUTE_TOKENS = (
    'execute', 'prepare', 'action', 'actions', 'payment', 'pay', 'submit',
    'upload', 'withdraw', 'cancel', 'renew', 'book', 'reserve', 'model',
    'generate', 'key', 'credential', 'config', 'secret', 'admin', 'proxy',
    'forward', 'download', 'file', 'raw', 'graphql',
)

# Exact key match (case-folded) on any depth.  A key is dropped, never renamed:
# the dashboard must not receive a value it could render by accident.
DENY_KEYS = frozenset({
    'cookie', 'cookies', 'set-cookie', 'token', 'tokens', 'access_token',
    'refresh_token', 'id_token', 'session', 'session_id', 'sid',
    'authorization', 'auth_header', 'password', 'passwd', 'secret', 'secrets',
    'api_key', 'apikey', 'credential', 'credentials', 'ticket', 'st',
    'csrf', 'csrf_token', 'member', 'members', 'member_id', 'member_name',
    'borrower', 'borrower_name', 'student_id', 'id_card', 'phone', 'email',
    'address', 'path', 'file_path', 'local_path', 'absolute_path',
    'storage_path', 'state_dir', 'home', 'user_dir', 'pid', 'owner_pid',
    'host', 'hostname', 'headers', 'request_headers', 'raw', 'raw_html',
    'raw_response', 'exception', 'traceback', 'stack', 'storage_key',
    'encryption_key', 'private_key', 'proxy_url', 'jwt', 'assertion',
})

FORBIDDEN_PARAMETER_KEYS = frozenset({
    'url', 'uri', 'href', 'method', 'headers', 'host', 'port', 'cookie',
    'cookies', 'token', 'authorization', 'auth', 'body', 'payload', 'proxy',
    'forward', 'download', 'path', 'file', 'redirect', 'session',
})

_PARAMETER_KEY = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{0,63}$')
_SERVICE = re.compile(r'^' + SERVICE_ID + r'$')
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_CURSOR = re.compile(r'^[A-Za-z0-9._~-]{1,128}$')

# Local-boundary error codes.  Registered in error-classification-v1 so a
# future caller of ``source_state.public_error`` does not collapse them.
ERROR_CATEGORIES = {
    'LOCAL_API_PUBLIC_BIND_REFUSED': 'INPUT',
    'LOCAL_API_PORT_INVALID': 'INPUT',
    'LOCAL_API_START_FAILED': 'INTERNAL',
    'LOCAL_API_HOST_NOT_ALLOWED': 'PERMISSION',
    'LOCAL_API_ORIGIN_NOT_ALLOWED': 'PERMISSION',
    'LOCAL_API_SESSION_REQUIRED': 'AUTH',
    'LOCAL_API_SESSION_EXPIRED': 'AUTH',
    'LOCAL_API_SESSION_INVALID': 'AUTH',
    'LOCAL_API_BOOTSTRAP_INVALID': 'AUTH',
    'LOCAL_API_BOOTSTRAP_EXPIRED': 'AUTH',
    'LOCAL_API_BOOTSTRAP_UNAVAILABLE': 'INTERNAL',
    'LOCAL_API_CSRF_REQUIRED': 'PERMISSION',
    'LOCAL_API_REQUEST_TOO_LARGE': 'INPUT',
    'LOCAL_API_REQUEST_BODY_NOT_ALLOWED': 'INPUT',
    'LOCAL_API_METHOD_NOT_ALLOWED': 'INPUT',
    'LOCAL_API_PATH_INVALID': 'INPUT',
    'LOCAL_API_ROUTE_NOT_FOUND': 'INPUT',
    'LOCAL_API_SERVICE_NOT_FOUND': 'INPUT',
    'LOCAL_API_QUERY_INVALID': 'INPUT',
    'LOCAL_API_PARAMETER_FORBIDDEN': 'INPUT',
    'LOCAL_API_PARAMETERS_TOO_LARGE': 'INPUT',
    'LOCAL_API_REFRESH_TOO_SOON': 'RATE_LIMIT',
    'LOCAL_API_READ_TIMEOUT': 'NETWORK',
    'LOCAL_API_FACADE_UNAVAILABLE': 'INTERNAL',
    'LOCAL_API_FACADE_INVALID_RESPONSE': 'UPSTREAM_CONTRACT',
    'LOCAL_API_RESPONSE_TOO_LARGE': 'COMPLETENESS',
    'LOCAL_API_INTERNAL_ERROR': 'INTERNAL',
    'LOCAL_API_UI_NOT_INSTALLED': 'NOT_IMPLEMENTED',
}

# Error statuses that must carry an ``error`` object (result-envelope-v1).
_FAILURE_STATUSES = source_state.ERROR_OBJECT_STATUSES

_ENVELOPE_KEYS = (
    'status', 'source', 'checked_at', 'verification_level',
    'source_complete', 'complete', 'next_cursor', 'warnings',
)


class LocalApiError(Exception):
    """A local boundary failure with a stable code and HTTP status."""

    def __init__(self, code: str, *, http_status: int = 400, message: str | None = None,
                 retry_after_ms: int | None = None, headers: Mapping[str, str] | None = None):
        known = code in ERROR_CATEGORIES or code in source_state.ERROR_CODES
        self.code = code if known else 'LOCAL_API_INTERNAL_ERROR'
        self.http_status = int(http_status)
        self.category = category_for(self.code)
        self.message = message
        self.retry_after_ms = retry_after_ms
        self.headers = dict(headers or {})
        super().__init__(self.code)


def category_for(code: str) -> str:
    """Category for a local code, or for an already registered shared code."""
    if code in ERROR_CATEGORIES:
        return ERROR_CATEGORIES[code]
    return source_state.ERROR_CODES.get(code, 'INTERNAL')


def is_date(value: Any) -> bool:
    return isinstance(value, str) and bool(_DATE.fullmatch(value))


def is_cursor(value: Any) -> bool:
    return isinstance(value, str) and bool(_CURSOR.fullmatch(value))


def is_service_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SERVICE.fullmatch(value))


def valid_parameter_key(value: Any) -> bool:
    return isinstance(value, str) and bool(_PARAMETER_KEY.fullmatch(value))


def route_for(path: str) -> tuple[Route | None, dict[str, str]]:
    """Return the frozen route for ``path`` and its path parameters."""
    for route in ROUTES:
        match = route.pattern.fullmatch(path)
        if match:
            return route, {key: value for key, value in match.groupdict().items() if value}
    return None, {}


def check_envelope(value: Any) -> list[str]:
    """Minimal structural check of a ``result-envelope-v1`` object.

    The local API never fabricates the missing half of a facade response: an
    incomplete envelope becomes an explicit ``LOCAL_API_FACADE_INVALID_RESPONSE``
    instead of an optimistic default.
    """
    if not isinstance(value, Mapping):
        return ['envelope is not an object']
    problems = [f'missing {key}' for key in _ENVELOPE_KEYS if key not in value]
    if problems:
        return problems
    status = value.get('status')
    if not isinstance(status, str) or status not in source_state.ENVELOPE_STATUSES:
        problems.append('status is not a result-envelope-v1 status')
    source = value.get('source')
    if not isinstance(source, Mapping):
        problems.append('source is not an object')
    else:
        for key in ('service_id', 'operation', 'adapter_version'):
            item = source.get(key)
            if not isinstance(item, str) or not item:
                problems.append(f'source.{key} is not a non-empty string')
    checked = value.get('checked_at')
    if not isinstance(checked, str):
        problems.append('checked_at is not a string')
    else:
        try:
            from datetime import datetime
            parsed = datetime.fromisoformat(checked.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                problems.append('checked_at has no UTC offset')
        except ValueError:
            problems.append('checked_at is not ISO 8601')
    if not isinstance(value.get('verification_level'), str):
        problems.append('verification_level is not a string')
    for key in ('source_complete', 'complete'):
        if type(value.get(key)) is not bool:
            problems.append(f'{key} is not a boolean')
    cursor = value.get('next_cursor')
    if cursor is not None and not isinstance(cursor, str):
        problems.append('next_cursor is neither null nor a string')
    warnings = value.get('warnings')
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        problems.append('warnings is not a list of strings')
    if value.get('complete') is True and value.get('source_complete') is not True:
        problems.append('complete is true while source_complete is false')
    if status == 'EMPTY' and (value.get('complete') is not True
                             or value.get('source_complete') is not True):
        problems.append('EMPTY must be a proven complete empty result')
    if status in _FAILURE_STATUSES:
        error = value.get('error')
        if not isinstance(error, Mapping) or not isinstance(error.get('code'), str):
            problems.append('a failure status has no error object')
        else:
            if not isinstance(error.get('category'), str):
                problems.append('error.category is missing')
            if error.get('automatic_replay_allowed') is not False:
                problems.append('error.automatic_replay_allowed must be false')
    return problems


def envelope_status_for_http(http_status: int) -> str:
    return {
        401: 'AUTH_REQUIRED',
        403: 'FORBIDDEN',
        404: 'NOT_FOUND',
        429: 'RATE_LIMITED',
        504: 'TIMEOUT',
    }.get(int(http_status), 'UNKNOWN' if int(http_status) >= 500 else 'ERROR')


def route_table() -> list[dict[str, Any]]:
    """The public route inventory consumed by the dashboard and its tests."""
    return [
        {
            'name': route.name,
            'methods': list(METHOD_ALLOWLIST),
            'path': BASE_PATH + route.template,
            'query': list(route.query),
            'read_only': True,
            'facade': route.facade,
        }
        for route in ROUTES
    ]


def display_states() -> list[str]:
    return ['READY', 'EMPTY', 'PARTIAL', 'LOGIN_REQUIRED', 'LOCAL_SESSION_REQUIRED',
            'LIMITED', 'FORBIDDEN', 'MAINTENANCE', 'ERROR', 'UNKNOWN']
