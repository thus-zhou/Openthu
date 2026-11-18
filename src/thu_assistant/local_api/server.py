"""Loopback-only, read-only HTTP surface for the local dashboard.

The server exposes exactly the seven frozen routes under ``/api/v1`` plus the
optional static shell owned by 7.1.  It has no prepare/execute/payment/upload/
model/key route, refuses every non-``GET``/``HEAD`` method before touching the
facade, and never forwards a request to an arbitrary URL.

Lifecycle: one instance owns one socket, one executor and one bootstrap file.
``stop()`` cancels the requests this instance holds, closes its own socket and
deletes its own file; it never signals, kills or inspects another process.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, unquote

from .. import paths as path_module
from .. import source_state
from . import contract, facade as facade_module, paging, projection, security

LOGGER = logging.getLogger('openthu.local_api')

STATIC_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
}
STATIC_MAX_BYTES = 4 * 1024 * 1024
JSON_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
HTML_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'none'; object-src 'none'")
COMMON_HEADERS = {
    'Cache-Control': 'no-store',
    'Pragma': 'no-cache',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY',
}


@dataclass
class _Request:
    method: str
    path: str
    raw_query: str
    headers: Mapping[str, str]
    body_length: int
    request_id: str
    route_name: str | None = None


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    route_name: str | None = None


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind a port another socket
    # is already listening on, so the "port busy" guard would silently hand two
    # boards the same port.  The Windows equivalent is SO_EXCLUSIVEADDRUSE.
    allow_reuse_address = os.name != 'nt'

    def __init__(self, address, handler, api: 'LocalApiServer'):
        self.api = api
        super().__init__(address, handler)

    def server_bind(self):
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):  # noqa: ARG002 - stdlib signature
        self.api.record_internal_error()


class _ThreadingServer6(_ThreadingServer):
    address_family = socket.AF_INET6


def _normalise_path(raw: str, limit: int) -> str:
    if not isinstance(raw, str) or not raw.startswith('/'):
        raise contract.LocalApiError('LOCAL_API_PATH_INVALID', http_status=400)
    if len(raw) > limit or any(ord(ch) < 0x20 for ch in raw) or '\\' in raw:
        raise contract.LocalApiError('LOCAL_API_PATH_INVALID', http_status=400)
    if '%' in raw and len(re.findall(r'%[0-9A-Fa-f]{2}', raw)) != raw.count('%'):
        raise contract.LocalApiError('LOCAL_API_PATH_INVALID', http_status=400)
    path = unquote(raw)
    if ('\x00' in path or '\\' in path or '//' in path
            or any(segment in ('.', '..') for segment in path.split('/'))):
        raise contract.LocalApiError('LOCAL_API_PATH_INVALID', http_status=400)
    return path


def _iso_now() -> str:
    return source_state.now_iso()


def _reject_json_constant(value: str) -> None:
    """RFC 8259 has no NaN/Infinity; the frozen facade does not accept them."""
    raise ValueError(value)


class LocalApiServer:
    """One process-owned local read API instance."""

    def __init__(self, facade: Any = None, *, bind: str = contract.DEFAULT_BIND,
                 port: int = 0, static_dir: Path | str | None = None,
                 state_dir: Path | str | None = None,
                 limits: contract.LocalApiLimits | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 logger: logging.Logger | None = None,
                 executor_workers: int = 8) -> None:
        if bind not in contract.ALLOWED_BINDS:
            raise contract.LocalApiError('LOCAL_API_PUBLIC_BIND_REFUSED', http_status=400,
                                         message='本地看板只允许监听回环地址。')
        if type(port) is not int or not 0 <= port <= 65535:
            raise contract.LocalApiError('LOCAL_API_PORT_INVALID', http_status=400)
        self.bind = bind
        self._requested_port = port
        self._limits = limits or contract.LocalApiLimits()
        self._clock = clock
        self._wall_clock = wall_clock
        self._logger = logger or LOGGER
        self._facade = facade if facade is not None else facade_module.UnavailableFacade()
        self._executor_workers = max(1, int(executor_workers))
        self._priority_workers = 2
        base_state = Path(state_dir) if state_dir is not None else path_module.default_paths().state_dir
        self.instance_id = secrets.token_hex(16)
        self._sessions = security.SessionStore(
            instance_id=self.instance_id, clock=clock,
            ttl=self._limits.session_ttl_seconds,
            idle=self._limits.session_idle_seconds,
            max_sessions=self._limits.max_sessions)
        self._bootstrap = security.BootstrapChannel(
            base_state, instance_id=self.instance_id, clock=wall_clock,
            ttl=self._limits.bootstrap_ttl_seconds)
        self._limiter = security.RateLimiter(
            limit=self._limits.max_requests_per_window,
            window=self._limits.request_window_seconds,
            refresh_min=self._limits.refresh_min_seconds, clock=clock)
        self._pages = paging.PageStore(clock=clock, ttl=self._limits.cursor_ttl_seconds,
                                       max_bytes=self._limits.max_response_bytes)
        self._static_dir = Path(static_dir).resolve() if static_dir is not None else None
        self._httpd: _ThreadingServer | None = None
        self._thread: threading.Thread | None = None
        self._read_executor: ThreadPoolExecutor | None = None
        self._priority_executor: ThreadPoolExecutor | None = None
        self._read_slots = threading.BoundedSemaphore(self._executor_workers)
        self._inflight: dict[str, facade_module.CancelToken] = {}
        self._inflight_lock = threading.Lock()
        self._stopping = threading.Event()
        self._started = False
        self._access: list[dict[str, Any]] = []
        self._internal_errors = 0
        self.port: int | None = None
        self.base_url: str | None = None
        self.bootstrap_file: Path | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> 'LocalApiServer':
        if self._started:
            return self
        if self._stopping.is_set():
            raise contract.LocalApiError('LOCAL_API_START_FAILED', http_status=500,
                                         message='该实例已经关闭，不能重新启动。')
        if self._static_dir is not None and not self._static_dir.is_dir():
            raise contract.LocalApiError('LOCAL_API_START_FAILED', http_status=500,
                                         message='静态资源目录不可用。')
        try:
            if self.bind == '::1':
                self._httpd = _ThreadingServer6((self.bind, self._requested_port, 0, 0),
                                                _Handler, self)
            else:
                self._httpd = _ThreadingServer((self.bind, self._requested_port),
                                              _Handler, self)
        except OSError:
            self._httpd = None
            raise contract.LocalApiError('LOCAL_API_START_FAILED', http_status=500,
                                         message='本地端口无法监听；请更换端口。') from None
        self.port = int(self._httpd.server_address[1])
        host = '127.0.0.1' if self.bind == '127.0.0.1' else '[::1]'
        self.base_url = f'http://{host}:{self.port}'
        self._read_executor = ThreadPoolExecutor(max_workers=self._executor_workers,
                                                 thread_name_prefix='openthu-local-read')
        self._priority_executor = ThreadPoolExecutor(max_workers=self._priority_workers,
                                                     thread_name_prefix='openthu-local-priority')
        try:
            self.bootstrap_file = self._bootstrap.publish(port=self.port,
                                                          base_url=self.base_url,
                                                          pid=os.getpid())
        except OSError:
            self._read_executor.shutdown(wait=False, cancel_futures=True)
            self._priority_executor.shutdown(wait=False, cancel_futures=True)
            self._read_executor = None
            self._priority_executor = None
            self._httpd.server_close()
            self._httpd = None
            raise contract.LocalApiError('LOCAL_API_START_FAILED', http_status=500,
                                         message='受保护的本机通道不可用。') from None
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={'poll_interval': 0.05},
                                        name='openthu-local-api', daemon=True)
        self._thread.start()
        self._started = True
        self._logger.info('local_api_started instance=%s port=%s', self.instance_id, self.port)
        return self

    def serve_forever(self) -> None:
        if not self._started:
            self.start()
        assert self._thread is not None
        self._thread.join()

    def stop(self, timeout: float = 2.0) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        with self._inflight_lock:
            tokens = list(self._inflight.values())
        for token in tokens:
            token.cancel()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        for executor in (self._read_executor, self._priority_executor):
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, float(timeout)))
        self._bootstrap.revoke()
        self._sessions.clear()
        self._pages.clear()
        self._limiter.clear()
        self._logger.info('local_api_stopped instance=%s', self.instance_id)

    def __enter__(self) -> 'LocalApiServer':
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    @property
    def inflight_count(self) -> int:
        with self._inflight_lock:
            return len(self._inflight)

    @property
    def access_log(self) -> list[dict[str, Any]]:
        return list(self._access)

    def record_internal_error(self) -> None:
        self._internal_errors += 1
        self._logger.warning('local_api_handler_error instance=%s', self.instance_id)

    def record_access(self, method: str, route: str | None, status: int,
                      request_id: str, started: float) -> None:
        entry = {'method': method, 'route': route or 'unmatched', 'status': int(status),
                 'request_id': request_id,
                 'duration_ms': int((self._clock() - started) * 1000)}
        self._access.append(entry)
        if len(self._access) > 200:
            del self._access[:len(self._access) - 200]
        self._logger.info('local_api_request method=%s route=%s status=%s request_id=%s duration_ms=%s',
                          entry['method'], entry['route'], entry['status'],
                          entry['request_id'], entry['duration_ms'])

    # -- request handling --------------------------------------------------

    def dispatch(self, request: _Request) -> Response:
        started = self._clock()
        try:
            response = self._dispatch(request)
        except contract.LocalApiError as error:
            response = self.error_response(error, request.request_id)
        except Exception:
            self.record_internal_error()
            response = self.error_response(
                contract.LocalApiError('LOCAL_API_INTERNAL_ERROR', http_status=500),
                request.request_id)
        request.route_name = getattr(response, 'route_name', request.route_name)
        self.record_access(request.method, request.route_name, response.status,
                           request.request_id, started)
        return response

    def error_response(self, error: contract.LocalApiError,
                       request_id: str) -> Response:
        body = projection.error_body(error.code, request_id=request_id,
                                     http_status=error.http_status,
                                     message=error.message,
                                     retry_after_ms=error.retry_after_ms)
        headers = self._json_headers(request_id)
        headers.update(error.headers)
        if error.http_status == 405:
            headers['Allow'] = ', '.join(contract.METHOD_ALLOWLIST)
        if error.retry_after_ms:
            headers['Retry-After'] = str(max(1, error.retry_after_ms // 1000))
        return Response(int(error.http_status), self._encode(body), headers)

    def _json_headers(self, request_id: str) -> dict[str, str]:
        return {**COMMON_HEADERS, 'Content-Type': 'application/json; charset=utf-8',
                'Content-Security-Policy': JSON_CSP, 'X-Request-Id': request_id}

    @staticmethod
    def _encode(body: Mapping[str, Any]) -> bytes:
        return json.dumps(body, ensure_ascii=False, allow_nan=False,
                          default=str).encode('utf-8')

    def _json(self, payload: Mapping[str, Any], request_id: str,
              status: int = 200, route_name: str | None = None) -> Response:
        body = self._encode(payload)
        if len(body) > self._limits.max_response_bytes:
            raise contract.LocalApiError(
                'LOCAL_API_RESPONSE_TOO_LARGE', http_status=502,
                message='结果超出本地显示上限，请缩小范围。')
        return Response(status, body, self._json_headers(request_id),
                        route_name=route_name)

    def _dispatch(self, request: _Request) -> Response:
        path = _normalise_path(request.path, self._limits.max_path_characters)
        host = request.headers.get('Host') if request.headers else None
        if not security.host_allowed(host, self.port or 0):
            raise contract.LocalApiError('LOCAL_API_HOST_NOT_ALLOWED', http_status=403)
        origin = request.headers.get('Origin') if request.headers else None
        if origin not in (None, '') and not security.origin_allowed(origin, self.port or 0):
            raise contract.LocalApiError('LOCAL_API_ORIGIN_NOT_ALLOWED', http_status=403)
        if request.method not in contract.METHOD_ALLOWLIST:
            raise contract.LocalApiError('LOCAL_API_METHOD_NOT_ALLOWED', http_status=405)
        if request.body_length > self._limits.request_body_bytes:
            raise contract.LocalApiError('LOCAL_API_REQUEST_TOO_LARGE', http_status=413)
        if request.body_length > 0:
            raise contract.LocalApiError('LOCAL_API_REQUEST_BODY_NOT_ALLOWED', http_status=400)
        site = request.headers.get('Sec-Fetch-Site') if request.headers else None
        mode = request.headers.get('Sec-Fetch-Mode') if request.headers else None
        if not security.fetch_metadata_allowed(site, mode):
            raise contract.LocalApiError('LOCAL_API_ORIGIN_NOT_ALLOWED', http_status=403)
        if path == '/' or path.startswith('/assets/'):
            return self._static(path, request)
        route, path_params = contract.route_for(path)
        if route is None:
            raise contract.LocalApiError('LOCAL_API_ROUTE_NOT_FOUND', http_status=404)
        session_id, cookie_header = self._authenticate(request)
        session_key = hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:16]
        retry = self._limiter.check_request(session_key)
        if retry is not None:
            raise contract.LocalApiError('LOCAL_API_RATE_LIMITED', http_status=429,
                                         retry_after_ms=retry)
        query = self._parse_query(request.raw_query, route)
        refresh_key = None
        if (route.name in ('query', 'todos', 'calendar', 'resources')
                and not query.get('cursor') and request.method != 'HEAD'):
            refresh_key = '|'.join((session_key, route.name,
                                    json.dumps(path_params, sort_keys=True),
                                    json.dumps({key: value for key, value in query.items()
                                                if key not in ('cursor', 'page_size')},
                                               sort_keys=True, default=str),
                                    str(query.get('page_size', self._limits.page_size_default))))
            retry = self._limiter.peek_refresh(refresh_key)
            if retry is not None:
                raise contract.LocalApiError('LOCAL_API_REFRESH_TOO_SOON', http_status=429,
                                             retry_after_ms=retry)
        response = self._read(route, path_params, query, request, session_key)
        if refresh_key is not None and response.status < 500:
            # A failed read keeps the slot free so the user can retry now.
            self._limiter.record_refresh(refresh_key)
        if cookie_header:
            response.headers['Set-Cookie'] = cookie_header
        return response

    def _authenticate(self, request: _Request) -> tuple[str, str | None]:
        header = request.headers.get('X-Openthu-Bootstrap') if request.headers else None
        if header:
            state = self._bootstrap.consume(header)
            if state == 'USED':
                raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_USED', http_status=401)
            if state == 'EXPIRED':
                raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_EXPIRED', http_status=401)
            if state != 'OK':
                raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID', http_status=401)
            sid = self._sessions.issue()
            return sid, self._sessions.cookie(sid)
        candidates = security.cookie_values(request.headers.get('Cookie')
                                            if request.headers else None)
        if not candidates:
            raise contract.LocalApiError('LOCAL_API_SESSION_REQUIRED', http_status=401)
        states = []
        for sid in candidates:
            state, _record = self._sessions.validate(sid)
            if state == 'OK':
                return sid, None
            states.append(state)
        if any(state in ('EXPIRED', 'IDLE_EXPIRED') for state in states):
            raise contract.LocalApiError('LOCAL_API_SESSION_EXPIRED', http_status=401)
        raise contract.LocalApiError('LOCAL_API_SESSION_INVALID', http_status=401)

    # -- parameters --------------------------------------------------------

    def _parse_query(self, raw: str, route: contract.Route) -> dict[str, Any]:
        if raw is None:
            raw = ''
        if len(raw) > self._limits.max_query_characters:
            raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400)
        try:
            pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                              encoding='utf-8', errors='strict')
        except (UnicodeDecodeError, ValueError):
            raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400) from None
        values: dict[str, Any] = {}
        for key, value in pairs:
            if key not in route.query:
                raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400)
            if key in values:
                raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400)
            values[key] = value
        page_size = values.get('page_size')
        if page_size is not None:
            if not re.fullmatch(r'[0-9]{1,3}', page_size):
                raise contract.LocalApiError('INVALID_ARGUMENT_TYPE', http_status=400)
            page_size = int(page_size)
            if not 1 <= page_size <= self._limits.page_size_max:
                raise contract.LocalApiError('INVALID_ARGUMENT_RANGE', http_status=400)
            values['page_size'] = page_size
        cursor = values.get('cursor')
        if cursor is not None and not contract.is_cursor(cursor):
            raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400)
        if 'parameters' in values:
            values['parameters'] = self._parse_parameters(values['parameters'])
        for key in ('query', 'category'):
            if key in values:
                values[key] = self._text_argument(values[key], 200)
        if 'semester' in values:
            semester = values['semester']
            if not isinstance(semester, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', semester):
                raise contract.LocalApiError('INVALID_ARGUMENT_VALUE', http_status=400)
        if 'course_id' in values:
            course_id = values['course_id']
            if not isinstance(course_id, str) or len(course_id) > 128 \
                    or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', course_id):
                raise contract.LocalApiError('INVALID_ARGUMENT_VALUE', http_status=400)
        for key in ('start_date', 'end_date'):
            if key in values:
                values[key] = self._date_argument(values[key])
        if 'start_date' in values and 'end_date' in values:
            start = date.fromisoformat(values['start_date'])
            end = date.fromisoformat(values['end_date'])
            if start > end or (end - start).days > 366:
                raise contract.LocalApiError('INVALID_DATE_RANGE', http_status=400)
        return values

    @staticmethod
    def _text_argument(value: str, limit: int) -> str:
        if not isinstance(value, str) or len(value) > limit or any(ord(ch) < 0x20 for ch in value):
            raise contract.LocalApiError('INVALID_ARGUMENT_VALUE', http_status=400)
        return value

    @staticmethod
    def _date_argument(value: str) -> str:
        if not contract.is_date(value):
            raise contract.LocalApiError('INVALID_ARGUMENT_VALUE', http_status=400)
        try:
            date.fromisoformat(value)
        except ValueError:
            raise contract.LocalApiError('INVALID_ARGUMENT_VALUE', http_status=400) from None
        return value

    def _parse_parameters(self, raw: str) -> dict[str, Any]:
        if not isinstance(raw, str) or len(raw) > self._limits.max_parameters_characters:
            raise contract.LocalApiError('LOCAL_API_PARAMETERS_TOO_LARGE', http_status=400)
        try:
            value = json.loads(raw, parse_constant=_reject_json_constant)
        except ValueError:
            raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400) from None
        if not isinstance(value, dict):
            raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400)
        self._check_parameter_value(value, depth=0)
        if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) \
                > self._limits.max_parameters_characters:
            raise contract.LocalApiError('LOCAL_API_PARAMETERS_TOO_LARGE', http_status=400)
        return value

    def _check_parameter_value(self, value: Any, *, depth: int) -> None:
        if depth > 4:
            raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400)
        if isinstance(value, dict):
            for key, item in value.items():
                if not contract.valid_parameter_key(key):
                    raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400)
                if key.casefold() in contract.FORBIDDEN_PARAMETER_KEYS:
                    raise contract.LocalApiError('LOCAL_API_PARAMETER_FORBIDDEN', http_status=400)
                self._check_parameter_value(item, depth=depth + 1)
        elif isinstance(value, list):
            if len(value) > 200:
                raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400)
            for item in value:
                self._check_parameter_value(item, depth=depth + 1)
        elif isinstance(value, str):
            if len(value) > 2000 or any(ord(ch) < 0x20 and ch not in '\n\t' for ch in value):
                raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400)
        elif isinstance(value, (bool, int, float)) or value is None:
            return
        else:
            raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400)

    # -- facade reads ------------------------------------------------------

    def _run_call(self, slot: threading.BoundedSemaphore | None, method: Any,
                  args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        try:
            return facade_module.call_with_token(method, *args, **kwargs)
        finally:
            if slot is not None:
                try:
                    slot.release()
                except ValueError:
                    pass

    def _call(self, name: str, *args: Any, request_id: str,
              priority: bool = False, **kwargs: Any) -> Any:
        try:
            method = getattr(self._facade, name)
        except AttributeError:
            raise contract.LocalApiError(
                'LOCAL_API_FACADE_UNAVAILABLE', http_status=501,
                message='业务门面未提供该读取能力。') from None
        token = facade_module.CancelToken()
        executor = self._priority_executor if priority else self._read_executor
        if executor is None:
            raise contract.LocalApiError('LOCAL_API_FACADE_UNAVAILABLE', http_status=502)
        slot = None
        if not priority:
            # Capacity is held by the worker, not by this request: a stuck
            # adapter keeps its slot, so the next expensive read fails fast
            # instead of queueing behind it.  Health/services use the separate
            # priority pool and stay available.
            if not self._read_slots.acquire(blocking=False):
                raise contract.LocalApiError('LOCAL_API_BUSY', http_status=503,
                                             message='本地读取容量已满，请稍后重试。')
            slot = self._read_slots
        try:
            future = executor.submit(self._run_call, slot, method, args,
                                     {**kwargs, 'cancel': token})
        except RuntimeError:
            if slot is not None:
                try:
                    slot.release()
                except ValueError:
                    pass
            raise contract.LocalApiError('LOCAL_API_FACADE_UNAVAILABLE', http_status=503,
                                         message='本地服务正在关闭。') from None
        with self._inflight_lock:
            self._inflight[request_id] = token
        try:
            return future.result(timeout=self._limits.ui_wait_seconds)
        except FuturesTimeout:
            token.cancel()
            future.cancel()
            raise contract.LocalApiError('LOCAL_API_READ_TIMEOUT', http_status=504,
                                         message='读取超时，其他卡片不受影响。') from None
        except contract.LocalApiError:
            raise
        except Exception as error:
            code = getattr(error, 'code', None)
            if code == 'CANCELLED':
                raise contract.LocalApiError('CANCELLED', http_status=503,
                                             message='本地请求已取消。') from None
            if code == 'SERVICE_NOT_FOUND_REFRESH_DIRECTORY_OR_USE_URL':
                raise contract.LocalApiError('LOCAL_API_SERVICE_NOT_FOUND', http_status=404,
                                             message='未在服务目录登记该服务。') from None
            # A facade client refusal is not a transport failure.  Preserve
            # its registered code and category instead of folding everything
            # into 502 LOCAL_API_FACADE_UNAVAILABLE (P2-2).
            facade_http = {
                'FACADE_SERVICE_UNKNOWN': 404,
                'FACADE_OPERATION_NOT_REGISTERED': 404,
                'FACADE_WRITE_NOT_REGISTERED': 404,
                'FACADE_QUERY_CANNOT_PROXY': 404,
                'FACADE_CAPABILITY_UNAVAILABLE': 409,
                'FACADE_REQUEST_LIMIT': 429,
                'ACTION_ACCOUNT_MISMATCH': 409,
            }
            if isinstance(code, str) and code in facade_http:
                raise contract.LocalApiError(code, http_status=facade_http[code]) from None
            self._logger.warning('local_api_facade_error instance=%s type=%s',
                                 self.instance_id, type(error).__name__)
            raise contract.LocalApiError('LOCAL_API_FACADE_UNAVAILABLE',
                                         http_status=502,
                                         message='业务门面不可用。') from None
        finally:
            with self._inflight_lock:
                self._inflight.pop(request_id, None)

    def _validate(self, envelope: Any, *, service_id: str, operation: str) -> dict[str, Any]:
        problems = contract.check_envelope(envelope)
        if not problems and isinstance(envelope, Mapping):
            source = envelope.get('source') if isinstance(envelope.get('source'), Mapping) else {}
            if source.get('service_id') != service_id:
                problems.append('source.service_id does not match the requested route')
            if source.get('operation') != operation:
                problems.append('source.operation does not match the requested operation')
        if problems:
            self._logger.warning('local_api_invalid_envelope instance=%s count=%s',
                                 self.instance_id, len(problems))
            raise contract.LocalApiError(
                'LOCAL_API_FACADE_INVALID_RESPONSE', http_status=502,
                message='业务门面返回了无法识别的结果。')
        return dict(envelope)

    def _account_scope(self) -> str:
        """Account dimension for the ``lac1`` paging key.

        A cursor must not survive a campus-account switch inside one local
        session.  The facade exposes ``account_scope()`` for this; a stub or a
        facade without an identity yields ``UNAVAILABLE`` and account-bound
        reads fail closed before an envelope exists (P2-11), so the fallback
        cannot expose another account's snapshot.
        """
        provider = getattr(self._facade, 'account_scope', None)
        if callable(provider):
            try:
                value = provider()
            except Exception:
                return 'UNAVAILABLE'
            if type(value) is int:
                return str(value)
            if isinstance(value, str) and value:
                return value
        return 'UNAVAILABLE'

    def _read(self, route: contract.Route, path_params: Mapping[str, str],
              query: Mapping[str, Any], request: _Request,
              session_key: str) -> Response:
        page_size = int(query.get('page_size', self._limits.page_size_default))
        cursor = query.get('cursor')
        key = paging.binding(route.name, path_params,
                             {k: v for k, v in query.items() if k != 'cursor'},
                             page_size, session_key,
                             account_scope=self._account_scope())
        service_id, operation = self._read_target(route, path_params)
        if cursor and isinstance(cursor, str) and cursor.startswith(paging.CURSOR_PREFIX + '.'):
            envelope = self._pages.resume(cursor, key)
            body = projection.project(envelope, service_id=service_id, operation=operation,
                                      request_id=request.request_id)
            return self._json(body, request.request_id, route_name=route.name)
        if cursor and route.name in ('todos', 'calendar'):
            # These two routes have no facade-native paging, so the only cursor
            # they issue is the local ``lac1`` token.  A foreign, expired or
            # fabricated value must be refused instead of silently returning the
            # first page again.
            raise contract.LocalApiError(
                'CURSOR_EXPIRED', http_status=400,
                message='该游标不属于此路由或已过期，请重新读取第一页。')
        if route.name == 'health':
            envelope = self._health_envelope(request)
        elif route.name == 'services':
            envelope = self._services_envelope(query, request)
        elif route.name == 'service_detail':
            envelope = self._detail_envelope(path_params['service_id'], request)
        elif route.name == 'query':
            envelope = self._call('query', path_params['service_id'],
                                  path_params['operation'],
                                  query.get('parameters') or {},
                                  cursor=cursor, page_size=page_size,
                                  request_id=request.request_id)
            envelope = self._validate(envelope, service_id=service_id,
                                      operation=operation)
        else:
            arguments: dict[str, Any] = {'cursor': cursor, 'page_size': page_size}
            if route.name in ('todos', 'calendar'):
                arguments['start_date'] = query.get('start_date')
                arguments['end_date'] = query.get('end_date')
            else:
                arguments['query'] = query.get('query')
                arguments['course_id'] = query.get('course_id')
                if route.name == 'resources':
                    arguments['semester'] = query.get('semester')
            envelope = self._call(route.facade or route.name,
                                  request_id=request.request_id, **arguments)
            envelope = self._validate(envelope, service_id=service_id,
                                      operation=operation)
        items = envelope.get('items')
        if route.name in ('query', 'todos', 'calendar', 'resources') \
                and isinstance(items, list) and len(items) > page_size:
            envelope = self._pages.start(key, envelope, items, page_size)
        body = projection.project(envelope, service_id=service_id, operation=operation,
                                  request_id=request.request_id)
        return self._json(body, request.request_id, route_name=route.name)

    @staticmethod
    def _read_target(route: contract.Route,
                     path_params: Mapping[str, str]) -> tuple[str, str]:
        if route.name == 'query':
            return path_params['service_id'], path_params['operation']
        if route.name in ('todos', 'calendar'):
            return 'agenda', route.name
        if route.name == 'resources':
            return 'resources', 'resources'
        if route.name == 'service_detail':
            return path_params['service_id'], 'describe'
        return 'local', route.name

    def _health_envelope(self, request: _Request) -> dict[str, Any]:
        health = self._call('health', request_id=request.request_id, priority=True)
        if not isinstance(health, Mapping):
            raise contract.LocalApiError('LOCAL_API_FACADE_INVALID_RESPONSE', http_status=502)
        status = health.get('status') if health.get('status') in source_state.ENVELOPE_STATUSES \
            else 'UNKNOWN'
        checked = source_state.normalize_timestamp(health.get('checked_at'), _iso_now())
        services = health.get('services') if isinstance(health.get('services'), list) else []
        complete = bool(services) and status in source_state.COMPLETE_STATUSES \
            and all(isinstance(row, Mapping) and row.get('complete') is True for row in services)
        return {
            'status': status,
            'source': {'service_id': 'local', 'operation': 'health',
                       'adapter_version': contract.CONTRACT_VERSION, 'url': None},
            'checked_at': checked,
            'verification_level': 'NONE',
            'source_complete': complete,
            'snapshot_complete': True,
            'complete': complete,
            'next_cursor': None,
            'warnings': [str(item) for item in (health.get('warnings') or [])
                         if isinstance(item, str) and 0 < len(item) <= 200],
            'items': [],
            'data': dict(health),
        }

    def _services_envelope(self, query: Mapping[str, Any], request: _Request) -> dict[str, Any]:
        envelope = self._call('services', query=query.get('query'),
                              category=query.get('category'),
                              request_id=request.request_id, priority=True)
        envelope = self._validate(envelope, service_id='local', operation='services')
        rows = facade_module.normalise_service_list(envelope.get('items'))
        envelope['items'] = rows
        envelope['returned_items'] = len(rows)
        if envelope.get('status') == 'OK' and not rows \
                and envelope.get('complete') is True and envelope.get('source_complete') is True:
            envelope['status'] = 'EMPTY'
        return envelope

    def _detail_envelope(self, service_id: str, request: _Request) -> dict[str, Any]:
        describe = self._call('describe', service_id, request_id=request.request_id,
                              priority=True)
        health = self._call('health', service_id=service_id, request_id=request.request_id,
                            priority=True)
        catalog = self._call('services', request_id=request.request_id, priority=True)
        if not isinstance(describe, Mapping) or not isinstance(health, Mapping):
            raise contract.LocalApiError('LOCAL_API_FACADE_INVALID_RESPONSE', http_status=502)
        status = health.get('status') if health.get('status') in source_state.ENVELOPE_STATUSES \
            else 'UNKNOWN'
        entry = None
        if isinstance(catalog, Mapping):
            for row in facade_module.normalise_service_list(catalog.get('items')):
                if row.get('id') == service_id:
                    entry = row
                    break
        actions = []
        for action in describe.get('actions') or []:
            if isinstance(action, Mapping):
                actions.append({**dict(action), 'executable_via': 'MCP_CLI_ONLY',
                                'local_route': None})
        checked = source_state.normalize_timestamp(health.get('checked_at'), _iso_now())
        rows = health.get('services') if isinstance(health.get('services'), list) else []
        complete = bool(rows) and status in source_state.COMPLETE_STATUSES \
            and all(isinstance(row, Mapping) and row.get('complete') is True for row in rows)
        return {
            'status': status,
            'source': {'service_id': 'local', 'operation': 'describe',
                       'adapter_version': contract.CONTRACT_VERSION, 'url': None},
            'checked_at': checked,
            'verification_level': 'NONE',
            'source_complete': complete,
            'snapshot_complete': True,
            'complete': complete,
            'next_cursor': None,
            'warnings': [str(item) for item in (health.get('warnings') or [])
                         if isinstance(item, str) and 0 < len(item) <= 200],
            'items': [],
            'data': {
                'service_id': service_id,
                'catalog': entry,
                'describe': dict(describe),
                'actions': actions,
                'health': dict(health),
                'write_routes': [],
            },
        }

    # -- static shell ------------------------------------------------------

    def _static(self, path: str, request: _Request) -> Response:
        if self._static_dir is None:
            raise contract.LocalApiError('LOCAL_API_UI_NOT_INSTALLED', http_status=404,
                                         message='本地界面资源尚未安装。')
        if path == '/':
            target = self._static_dir / 'index.html'
        else:
            relative = path[len('/assets/'):]
            if not relative or len(relative) > 512:
                raise contract.LocalApiError('LOCAL_API_PATH_INVALID', http_status=400)
            segments = relative.split('/')
            if any(not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', segment)
                   for segment in segments):
                raise contract.LocalApiError('LOCAL_API_PATH_INVALID', http_status=400)
            target = self._static_dir / 'assets' / Path(*segments)
        suffix = target.suffix.lower()
        if suffix not in STATIC_TYPES:
            raise contract.LocalApiError('LOCAL_API_ROUTE_NOT_FOUND', http_status=404)
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            raise contract.LocalApiError('LOCAL_API_ROUTE_NOT_FOUND', http_status=404) from None
        if not path_module.is_within(resolved, self._static_dir) or not resolved.is_file():
            raise contract.LocalApiError('LOCAL_API_ROUTE_NOT_FOUND', http_status=404)
        try:
            if resolved.stat().st_size > STATIC_MAX_BYTES:
                raise contract.LocalApiError('LOCAL_API_ROUTE_NOT_FOUND', http_status=404)
            body = resolved.read_bytes()
        except OSError:
            raise contract.LocalApiError('LOCAL_API_ROUTE_NOT_FOUND', http_status=404) from None
        headers = {**COMMON_HEADERS,
                   'Content-Type': STATIC_TYPES[suffix],
                   'Content-Security-Policy': HTML_CSP if suffix == '.html' else JSON_CSP}
        response = Response(200, body, headers, route_name='static')
        if suffix == '.html' and security.trusted_navigation(
                request.headers.get('Origin'),
                request.headers.get('Sec-Fetch-Site'),
                request.headers.get('Sec-Fetch-Mode')):
            session_id, _ = self._session_for_navigation(request)
            if session_id:
                response.headers['Set-Cookie'] = self._sessions.cookie(session_id)
        return response

    def _session_for_navigation(self, request: _Request) -> tuple[str | None, None]:
        for sid in security.cookie_values(request.headers.get('Cookie')
                                          if request.headers else None):
            if self._sessions.validate(sid)[0] == 'OK':
                return sid, None
        try:
            return self._sessions.issue(), None
        except contract.LocalApiError:
            # The shell is data-free; reaching the session cap must not turn it
            # into a server error.
            return None, None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'OpenthuLocalApi/' + contract.CONTRACT_VERSION
    sys_version = ''

    def setup(self) -> None:
        super().setup()
        try:
            self.request.settimeout(15.0)
        except OSError:
            pass

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    def log_error(self, format, *args):  # noqa: A002 - stdlib signature
        return

    def do_GET(self):
        self._run('GET')

    def do_HEAD(self):
        self._run('HEAD')

    def do_POST(self):
        self._run('POST')

    def do_PUT(self):
        self._run('PUT')

    def do_PATCH(self):
        self._run('PATCH')

    def do_DELETE(self):
        self._run('DELETE')

    def do_OPTIONS(self):
        self._run('OPTIONS')

    def do_TRACE(self):
        self._run('TRACE')

    def _run(self, method: str) -> None:
        api: LocalApiServer = self.server.api  # type: ignore[attr-defined]
        request_id = secrets.token_hex(16)
        started = api._clock()
        try:
            length = self._content_length()
            query = self.path.partition('?')[2]
            path = self.path.partition('?')[0]
            request = _Request(method=method, path=path, raw_query=query,
                               headers=self.headers, body_length=length,
                               request_id=request_id)
            response = api.dispatch(request)
        except contract.LocalApiError as error:
            response = api.error_response(error, request_id)
        except Exception:
            api.record_internal_error()
            response = api.error_response(
                contract.LocalApiError('LOCAL_API_INTERNAL_ERROR', http_status=500),
                request_id)
        try:
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(response.body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            if method != 'HEAD' and response.body:
                self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            self.close_connection = True

    def _content_length(self) -> int:
        if self.headers.get('Transfer-Encoding'):
            raise contract.LocalApiError('LOCAL_API_REQUEST_BODY_NOT_ALLOWED', http_status=400)
        raw = self.headers.get('Content-Length')
        if raw is None:
            return 0
        if not isinstance(raw, str) or not raw.strip().isdigit():
            raise contract.LocalApiError('LOCAL_API_REQUEST_BODY_NOT_ALLOWED', http_status=400)
        return int(raw.strip())

    def send_error(self, code, message=None, explain=None):  # noqa: ARG002
        request_id = secrets.token_hex(16)
        api: LocalApiServer = self.server.api  # type: ignore[attr-defined]
        error = contract.LocalApiError(
            'LOCAL_API_QUERY_INVALID' if int(code) < 500 else 'LOCAL_API_INTERNAL_ERROR',
            http_status=int(code) if 400 <= int(code) < 600 else 400)
        response = api.error_response(error, request_id)
        try:
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(response.body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            self.close_connection = True
