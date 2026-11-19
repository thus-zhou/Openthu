"""Local-boundary security: bind, Host/Origin, sessions, bootstrap and limits.

Threat model
------------
The dangerous caller is a *remote web page* in the user's browser, not another
process of the same OS account (which can already read the profile).  So the
boundary enforces, in order:

* loopback-only binding, with ``0.0.0.0``/``::`` refused before a socket opens;
* an exact ``Host`` allowlist, which defeats DNS rebinding;
* ``Origin``/``Sec-Fetch-Site`` checks, which defeat cross-site fetches and
  cross-site navigations;
* a process-bound session established either by a trusted local navigation or
  by a short-lived bootstrap token handed over through a 0600 file - never in a
  URL, a log line or the shared configuration;
* ``GET``/``HEAD`` only, a 64 KiB body ceiling, and a per-session rate limit.

The bootstrap file contains no account, campus session or business data: it is
deleted when the instance stops, and a file from another instance is ignored.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from . import contract

BOOTSTRAP_SCHEMA = 'openthu-local-api-bootstrap-v1'
COOKIE_NAME = 'openthu_local_session'
COOKIE_PATH = '/'


def split_host_header(value: str | None) -> tuple[str | None, int | None]:
    """Split ``host[:port]`` without trusting the text."""
    if not isinstance(value, str):
        return None, None
    text = value.strip()
    if not text or len(text) > 300 or any(ch in text for ch in '\r\n\t /\\@'):
        return None, None
    if text.startswith('['):
        end = text.find(']')
        if end < 0:
            return None, None
        host, rest = text[:end + 1].lower(), text[end + 1:]
        if rest.startswith(':'):
            port = rest[1:]
        elif rest:
            return None, None
        else:
            port = None
    else:
        host, separator, port = text.rpartition(':')
        if not separator:
            host, port = text, None
        elif not port.isdigit():
            return None, None
    if port is not None:
        if not port.isdigit() or not 0 < int(port) <= 65535:
            return None, None
        port = int(port)
    return host.lower(), port


def host_allowed(value: str | None, port: int) -> bool:
    host, header_port = split_host_header(value)
    return host in contract.HOST_ALLOWLIST and header_port == port


def origin_allowed(value: str | None, port: int) -> bool:
    """Only the exact same loopback origin, in its ``http://`` form."""
    if not isinstance(value, str) or not value or len(value) > 300:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed_port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == 'http'
            and host in ('127.0.0.1', 'localhost', '::1')
            and parsed_port == port
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ('', '/')
            and not parsed.query
            and not parsed.fragment)


def fetch_metadata_allowed(site: str | None, mode: str | None) -> bool:
    if site not in (None, '', 'same-origin', 'none'):
        return False
    if mode not in (None, '', 'navigate', 'cors', 'same-origin'):
        return False
    return True


def trusted_navigation(origin: str | None, site: str | None, mode: str | None) -> bool:
    """A browser navigation the user (or the launcher) started locally.

    ``Sec-Fetch-Site: none`` is what a typed/bookmarked/launcher-opened URL
    sends; a link from another page sends ``cross-site`` and is refused.
    """
    return (origin in (None, '')
            and (site in (None, '', 'none', 'same-origin'))
            and (mode in (None, '', 'navigate')))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class SessionStore:
    """Process-local opaque sessions; a restart invalidates every cookie."""

    def __init__(self, *, instance_id: str, clock: Callable[[], float] = time.monotonic,
                 ttl: float = contract.LIMITS['session_ttl_seconds'],
                 idle: float = contract.LIMITS['session_idle_seconds'],
                 max_sessions: int = contract.LIMITS['max_sessions'],
                 secret: bytes | None = None) -> None:
        self.instance_id = instance_id
        self._clock = clock
        self._ttl = float(ttl)
        self._idle = float(idle)
        self._max = max(1, int(max_sessions))
        self._secret = secret or secrets.token_bytes(32)
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _purge_locked(self) -> None:
        now = self._clock()
        for key in [key for key, row in self._sessions.items()
                    if now >= row['expires']]:
            self._sessions.pop(key, None)

    def issue(self) -> str:
        """Mint a session; never evict an active one to make room."""
        with self._lock:
            self._purge_locked()
            if len(self._sessions) >= self._max:
                raise contract.LocalApiError('LOCAL_API_SESSION_LIMIT', http_status=429,
                                             message='本地会话数量已达上限，请关闭多余会话。')
            sid = secrets.token_urlsafe(32)
            now = self._clock()
            self._sessions[_digest(sid)] = {'created': now, 'seen': now,
                                            'expires': now + self._ttl,
                                            'instance_id': self.instance_id}
            return sid

    def validate(self, sid: str | None) -> tuple[str, dict[str, Any] | None]:
        if not isinstance(sid, str) or not 20 <= len(sid) <= 200:
            return 'UNKNOWN', None
        key = _digest(sid)
        with self._lock:
            record = self._sessions.get(key)
            if record is None:
                return 'UNKNOWN', None
            now = self._clock()
            if record.get('instance_id') != self.instance_id or now >= record['expires']:
                self._sessions.pop(key, None)
                return 'EXPIRED', None
            if now - record['seen'] > self._idle:
                self._sessions.pop(key, None)
                return 'IDLE_EXPIRED', None
            record['seen'] = now
            return 'OK', dict(record)

    def csrf_for(self, sid: str) -> str | None:
        with self._lock:
            if _digest(sid) not in self._sessions:
                return None
        return hmac.new(self._secret, _digest(sid).encode('ascii'),
                        hashlib.sha256).hexdigest()[:32]

    def check_csrf(self, sid: str, value: str | None) -> bool:
        expected = self.csrf_for(sid)
        if not expected or not isinstance(value, str):
            return False
        return hmac.compare_digest(expected, value)

    def cookie(self, sid: str, *, max_age: int | None = None) -> str:
        age = int(self._ttl if max_age is None else max_age)
        # No Domain, no Secure: the cookie only ever travels over loopback HTTP
        # and ``Secure`` would make some browsers drop it.  HttpOnly and
        # SameSite=Strict are the protections this origin can actually use.
        return (f'{COOKIE_NAME}={sid}; Path={COOKIE_PATH}; Max-Age={age}; '
                f'HttpOnly; SameSite=Strict')

    def snapshot(self) -> list[dict[str, Any]]:
        """Metadata only; the raw session id is never stored."""
        with self._lock:
            return [dict(row) for row in self._sessions.values()]

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


def cookie_values(header: str | None) -> list[str]:
    """All values of our cookie name, in header order.

    A loopback page on another port can write a same-name cookie (cookies are
    host-scoped, not port-scoped).  Treating only the first value as the session
    would let that page lock the dashboard out; every candidate is verified
    instead.
    """
    if not isinstance(header, str) or len(header) > 8192:
        return []
    values = []
    for part in header.split(';'):
        name, separator, value = part.strip().partition('=')
        if separator and name == COOKIE_NAME:
            candidate = value.strip()
            if candidate:
                values.append(candidate)
    return values


def cookie_value(header: str | None) -> str | None:
    """The first candidate, kept for callers that only need one value."""
    values = cookie_values(header)
    return values[0] if values else None


class BootstrapChannel:
    """The protected local handover of a single-use bootstrap token."""

    def __init__(self, state_dir: Path | str, *, instance_id: str,
                 clock: Callable[[], float] = time.time,
                 ttl: float = contract.LIMITS['bootstrap_ttl_seconds'],
                 token: str | None = None) -> None:
        self.instance_id = instance_id
        self.directory = Path(state_dir) / 'local-api'
        self.path = self.directory / f'bootstrap-{instance_id}.json'
        self.token = token or secrets.token_urlsafe(32)
        self._clock = clock
        self._ttl = float(ttl)
        self.expires_at: float | None = None
        self._used = False
        self._lock = threading.RLock()

    def publish(self, *, port: int, base_url: str, pid: int) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        self.expires_at = self._clock() + self._ttl
        payload = {
            'schema': BOOTSTRAP_SCHEMA,
            'instance_id': self.instance_id,
            'port': int(port),
            'base_url': base_url,
            'token': self.token,
            'pid': int(pid),
            'created_at': _iso(self._clock()),
            'expires_at': _iso(self.expires_at),
            'api_version': contract.CONTRACT_VERSION,
        }
        temp = self.directory / f'.{self.path.name}.{os.getpid()}.tmp'
        try:
            temp.unlink(missing_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
            handle = os.open(temp, flags, 0o600)
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return self.path

    def expired(self) -> bool:
        return self.expires_at is None or self._clock() >= self.expires_at

    def verify(self, token: str | None) -> str:
        """Return ``OK``, ``USED``, ``EXPIRED`` or ``INVALID``."""
        with self._lock:
            if self._used:
                return 'USED'
            if self.expired():
                return 'EXPIRED'
            if not isinstance(token, str) or not 20 <= len(token) <= 200:
                return 'INVALID'
            return 'OK' if hmac.compare_digest(self.token, token) else 'INVALID'

    def consume(self, token: str | None) -> str:
        """Verify and atomically spend the token; a second exchange fails."""
        with self._lock:
            state = self.verify(token)
            if state == 'OK':
                self._used = True
                self.revoke()
            return state

    def revoke(self) -> None:
        with self._lock:
            try:
                record = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                record = {}
            if isinstance(record, dict) and record.get('instance_id') == self.instance_id:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec='seconds')


def _read_private_bootstrap(path: Path) -> bytes:
    """Read from one verified descriptor; never follow a symlink."""
    if os.name != 'posix' and os.path.islink(path):
        raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID', http_status=401)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_UNAVAILABLE',
                                     http_status=401) from None
    except OSError:
        raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID',
                                     http_status=401) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID', http_status=401)
        if os.name == 'posix':
            if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.geteuid():
                raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID', http_status=401)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 65536:
                raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID', http_status=401)
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        os.close(descriptor)


def read_bootstrap(path: Path | str, *,
                   clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Read and validate a bootstrap file from the protected local channel."""
    target = Path(path)
    data = _read_private_bootstrap(target)
    try:
        record = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID', http_status=401) from None
    if not isinstance(record, dict) or record.get('schema') != BOOTSTRAP_SCHEMA:
        raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID', http_status=401)
    token = record.get('token')
    port = record.get('port')
    base_url = record.get('base_url')
    if (not isinstance(token, str) or not 20 <= len(token) <= 200
            or type(port) is not int or not 0 < port <= 65535
            or not isinstance(base_url, str) or not base_url.startswith('http://')):
        raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID', http_status=401)
    try:
        expires = datetime.fromisoformat(str(record.get('expires_at')).replace('Z', '+00:00'))
    except ValueError:
        raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID', http_status=401) from None
    if expires.tzinfo is None:
        raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_INVALID', http_status=401)
    if clock() >= expires.timestamp():
        raise contract.LocalApiError('LOCAL_API_BOOTSTRAP_EXPIRED', http_status=401)
    return record


class RateLimiter:
    """A sliding-window request limit plus a per-resource refresh interval."""

    def __init__(self, *, limit: int = contract.LIMITS['max_requests_per_window'],
                 window: float = contract.LIMITS['request_window_seconds'],
                 refresh_min: float = contract.LIMITS['refresh_min_seconds'],
                 clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 1024) -> None:
        self._limit = max(1, int(limit))
        self._window = max(0.001, float(window))
        self._refresh_min = max(0.0, float(refresh_min))
        self._clock = clock
        self._max_keys = max(16, int(max_keys))
        self._hits: dict[str, list[float]] = {}
        self._refresh: dict[str, float] = {}
        self._lock = threading.RLock()

    def _prune_locked(self, now: float) -> None:
        for key in [key for key, hits in self._hits.items()
                    if not hits or now - hits[-1] >= self._window]:
            self._hits.pop(key, None)
        if len(self._refresh) > self._max_keys:
            for key in sorted(self._refresh, key=self._refresh.get)[:len(self._refresh) // 2]:
                self._refresh.pop(key, None)

    def check_request(self, key: str) -> int | None:
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            hits = self._hits.setdefault(key, [])
            hits[:] = [stamp for stamp in hits if now - stamp < self._window]
            if len(hits) >= self._limit:
                return max(1, int(math.ceil((hits[0] + self._window - now) * 1000)))
            hits.append(now)
            return None

    def peek_refresh(self, key: str) -> int | None:
        """Return a retry delay without consuming the refresh slot."""
        if self._refresh_min <= 0:
            return None
        with self._lock:
            now = self._clock()
            last = self._refresh.get(key)
            if last is not None and now - last < self._refresh_min:
                return max(1, int(math.ceil((self._refresh_min - (now - last)) * 1000)))
            return None

    def record_refresh(self, key: str) -> None:
        """Commit the slot only after a non-5xx read."""
        if self._refresh_min <= 0:
            return
        with self._lock:
            self._prune_locked(self._clock())
            self._refresh[key] = self._clock()

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
            self._refresh.clear()


def request_fingerprint(headers: Mapping[str, str]) -> str:
    """A non-reversible client hint used only for the rate-limit bucket."""
    raw = '|'.join(str(headers.get(name, '')) for name in ('User-Agent',))
    return hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()[:16]
