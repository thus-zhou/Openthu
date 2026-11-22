"""Local automated client: bootstrap over the protected file channel, then GET.

Used by module tests and by the dashboard launcher (7.1).  It speaks only to a
loopback origin, keeps the session cookie in memory, and never puts the
bootstrap token in a URL or a log line.
"""
from __future__ import annotations

from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit

from . import contract, security


class LocalApiClient:
    def __init__(self, base_url: str, *, token: str | None = None,
                 timeout: float = contract.LIMITS['ui_wait_seconds']) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('the local client only connects to a loopback origin')
        if parsed.port is None:
            raise ValueError('the local client needs an explicit loopback port')
        host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
        self.base_url = f'http://{host}:{parsed.port}'
        self.hostname = parsed.hostname
        self.port = int(parsed.port)
        self._bootstrap_token = token
        self.timeout = float(timeout)
        self.cookie: str | None = None
        self.last_headers: Mapping[str, str] = {}

    @classmethod
    def from_bootstrap(cls, path: Path | str, *,
                       clock: Callable[[], float] = time.time,
                       timeout: float = contract.LIMITS['ui_wait_seconds']) -> 'LocalApiClient':
        record = security.read_bootstrap(path, clock=clock)
        client = cls(record['base_url'], token=record['token'], timeout=timeout)
        return client

    def _connection(self) -> HTTPConnection:
        return HTTPConnection(self.hostname, self.port, timeout=self.timeout)

    def request(self, method: str, path: str,
                params: Mapping[str, Any] | None = None,
                *, bootstrap: bool = False) -> tuple[int, dict[str, Any]]:
        if not path.startswith('/'):
            raise ValueError('path must be absolute on the local origin')
        query = urlencode({key: value for key, value in (params or {}).items()
                           if value is not None}, doseq=True)
        target = path + (('?' + query) if query else '')
        headers = {'Accept': 'application/json'}
        if bootstrap and self._bootstrap_token:
            headers['X-Openthu-Bootstrap'] = self._bootstrap_token
        if self.cookie:
            headers['Cookie'] = f'{security.COOKIE_NAME}={self.cookie}'
        connection = self._connection()
        try:
            connection.request(method, target, headers=headers)
            response = connection.getresponse()
            body = response.read()
            self.last_headers = {name: value for name, value in response.getheaders()}
            cookie = response.getheader('Set-Cookie')
            if cookie and security.COOKIE_NAME + '=' in cookie:
                pair = cookie.split(';', 1)[0].strip()
                if pair.startswith(security.COOKIE_NAME + '='):
                    self.cookie = pair.split('=', 1)[1]
            if not body:
                return response.status, {}
            try:
                payload = json.loads(body.decode('utf-8'))
            except (UnicodeDecodeError, ValueError):
                raise HTTPException(f'non-JSON response from the local API ({response.status})')
            return response.status, payload if isinstance(payload, dict) else {'data': payload}
        finally:
            connection.close()

    def bootstrap(self) -> dict[str, Any]:
        """Establish a session through the 0600 bootstrap channel."""
        if not self._bootstrap_token:
            raise ValueError('a bootstrap token is required')
        status, payload = self.request('GET', contract.BASE_PATH + '/health', bootstrap=True)
        if status != 200 or not self.cookie:
            raise HTTPException(f'local session bootstrap failed ({status})')
        return payload

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        return self.request('GET', path, params)

    def close(self) -> None:
        self.cookie = None
        self._bootstrap_token = None
