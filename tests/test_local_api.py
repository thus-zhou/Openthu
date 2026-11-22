"""Module 6.2 tests: local read API routes, auth, projection and process bounds.

Everything is synthetic and local.  No campus service, model endpoint or
non-loopback address is contacted; each fixture facade counts the calls it
receives so a rejected request can be proven not to reach the business facade.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import quote, urlencode

import jsonschema
import pytest

from thu_assistant import local_api, source_state
from thu_assistant.bridge import CampusError
from thu_assistant.local_api import contract, paging, projection, security
from thu_assistant.local_api import samples as sample_module
from thu_assistant.local_api.client import LocalApiClient
from thu_assistant.local_api.server import Response
from thu_assistant.secure_store import SecureKeyPermissionError
from thu_assistant.secure_store import keys as secure_keys

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / 'contracts' / 'v0.2'
SOURCE = ROOT / 'src' / 'thu_assistant' / 'local_api'
ENVELOPE_SCHEMA = json.loads((CONTRACTS / 'result-envelope.schema.json').read_text(encoding='utf-8'))
API_CONTRACT = json.loads((CONTRACTS / 'local-read-api.schema.json').read_text(encoding='utf-8'))


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeWallClock(FakeClock):
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        super().__init__(now)


def valid_envelope(service: str, operation: str, *, status: str = 'OK',
                   items: list | None = None, complete: bool = True,
                   source_complete: bool | None = None) -> dict:
    envelope = {
        'status': status,
        'source': {'service_id': service, 'operation': operation,
                   'adapter_version': 'fixture-v1',
                   'url': 'https://learn.tsinghua.edu.cn/'},
        'checked_at': '2026-11-17T12:00:00+08:00',
        'verification_level': 'BUSINESS_READ',
        'source_complete': complete if source_complete is None else source_complete,
        'snapshot_complete': True,
        'complete': complete,
        'next_cursor': None,
        'warnings': [],
        'items': items or [],
        'query_time': '2026-11-17T11:59:58+08:00',
    }
    if status in source_state.ERROR_OBJECT_STATUSES:
        envelope['error'] = source_state.public_error(status)
    return envelope


class FixtureFacade:
    """Explicit synthetic facade; records every method it actually receives."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.delay: dict[str, float] = {}
        self.errors: dict[str, BaseException] = {}
        self.responses: dict[str, object] = {}
        self.account = 'fixture-account'
        self.health_rows = [
            {'service_id': 'learn', 'status': 'OK', 'checked_at': '2026-11-17T12:00:00+08:00',
             'complete': True},
        ]

    def account_scope(self):
        """The campus account this fixture would read as (P2-6 binding)."""
        return self.account

    def _resolve(self, name: str, default, cancel=None):
        self.calls.append(name)
        delay = float(self.delay.get(name, 0))
        if delay:
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if cancel is not None and getattr(cancel, 'cancelled', False):
                    raise CampusError('CANCELLED')
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        if name in self.errors:
            raise self.errors[name]
        if name in self.responses:
            value = self.responses[name]
            return deepcopy(value)
        return default() if callable(default) else deepcopy(default)

    def health(self, *, service_id=None, cancel=None):
        return self._resolve('health', {
            'status': 'OK', 'checked_at': '2026-11-17T12:00:00+08:00',
            'services': deepcopy(self.health_rows), 'warnings': [],
        }, cancel)

    def services(self, *, query=None, category=None, cancel=None):
        rows = sample_module.services_sample()['items']
        if query:
            rows = [row for row in rows if query in row['name'] or query in row['id']]
        if category:
            rows = [row for row in rows if category in row['category']]
        envelope = valid_envelope('local', 'services', items=rows)
        envelope['items'] = rows
        return self._resolve('services', envelope, cancel)

    def query(self, service_id, operation, parameters, *, cursor=None, page_size=50, cancel=None):
        rows = [{'course_id': f'SYNTHETIC-COURSE-{index}',
                 'course_name': f'合成课程 {index}',
                 'semester': 'SYNTHETIC-TERM'} for index in range(1, 6)]
        return self._resolve('query', valid_envelope(service_id, operation, items=rows), cancel)

    def describe(self, service_id, cancel=None):
        value = sample_module.service_detail_sample()['data']
        return self._resolve('describe', {
            'service_id': service_id,
            'operations': value['describe']['operations'],
            'actions': value['describe']['actions'],
            'registry_version': 'business-facade-v1',
        }, cancel)

    def todos(self, *, start_date=None, end_date=None, cursor=None, page_size=50, cancel=None):
        rows = [{'event_id': f'SYNTHETIC-EVENT-{index}', 'title': f'合成事项 {index}',
                 'source_id': 'learn.assignments', 'status': 'OPEN'} for index in range(1, 6)]
        return self._resolve('todos', valid_envelope('agenda', 'todos', items=rows), cancel)

    def calendar(self, *, start_date=None, end_date=None, cursor=None, page_size=50, cancel=None):
        value = sample_module.calendar_sample()
        return self._resolve('calendar', value, cancel)

    def resources(self, *, query=None, course_id=None, semester=None, cursor=None,
                  page_size=50, cancel=None):
        value = sample_module.resources_sample()
        value['items'][0]['official_url'] = 'https://learn.tsinghua.edu.cn/course/1'
        value['items'][0]['review_url'] = 'https://evil.example/phish'
        value['items'][0]['entry_url'] = 'https://learn.tsinghua.edu.cn/cas/login?service=x'
        return self._resolve('resources', value, cancel)


def make_server(*, facade=None, static_dir=None, state_dir=None, limits=None,
                clock=None, wall_clock=None, bind='127.0.0.1') -> local_api.LocalApiServer:
    server = local_api.LocalApiServer(
        facade if facade is not None else FixtureFacade(),
        static_dir=static_dir, state_dir=state_dir, limits=limits,
        clock=clock or FakeClock(), wall_clock=wall_clock or FakeWallClock(),
        bind=bind)
    return server.start()


@pytest.fixture
def facade():
    return FixtureFacade()


@pytest.fixture
def server(tmp_path, facade):
    instance = make_server(facade=facade, state_dir=tmp_path / 'state')
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def client(server, facade):
    value = LocalApiClient.from_bootstrap(server.bootstrap_file, clock=FakeWallClock())
    value.bootstrap()
    facade.calls.clear()  # bootstrap legitimately reads /health once
    return value


def raw_request(server, method: str, path: str, *, headers=None, body: bytes | None = None,
                host_header: str | None = None, timeout: float = 5.0):
    connection = http.client.HTTPConnection('127.0.0.1', server.port, timeout=timeout)
    try:
        connection.putrequest(method, path, skip_host=host_header is not None,
                              skip_accept_encoding=True)
        if host_header is not None:
            connection.putheader('Host', host_header)
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        if body is not None:
            connection.putheader('Content-Length', str(len(body)))
            connection.endheaders(body)
        else:
            connection.endheaders()
        response = connection.getresponse()
        return response.status, {name: value for name, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def json_request(server, method: str, path: str, *, headers=None, body=None,
                 host_header=None, timeout: float = 5.0):
    status, response_headers, payload = raw_request(
        server, method, path, headers=headers, body=body,
        host_header=host_header, timeout=timeout)
    decoded = json.loads(payload.decode('utf-8')) if payload else {}
    return status, response_headers, decoded


def auth_headers(client) -> dict:
    return {'Cookie': f'{security.COOKIE_NAME}={client.cookie}'}


def query_path(service='learn', operation='courses', parameters=None, **extra) -> str:
    from urllib.parse import urlencode
    params = {'parameters': json.dumps(parameters or {}, separators=(',', ':'))}
    params.update({key: value for key, value in extra.items() if value is not None})
    return f'/api/v1/query/{service}/{operation}?' + urlencode(params)


def validate_envelope(payload: dict) -> None:
    jsonschema.Draft202012Validator(ENVELOPE_SCHEMA).validate(payload)


# ---------------------------------------------------------------------------
# Frozen contract and route inventory
# ---------------------------------------------------------------------------

def test_route_table_matches_the_frozen_local_read_api_contract() -> None:
    frozen = {(endpoint['method'], API_CONTRACT['base_path'] + endpoint['path'])
              for endpoint in API_CONTRACT['endpoints']}
    implemented = {(method, route['path']) for route in contract.route_table()
                   for method in route['methods']}
    assert {(method, path) for method, path in implemented if method == 'GET'} == frozen
    assert {method for method, _path in implemented} == {'GET', 'HEAD'}


def test_query_parameters_equal_the_frozen_list() -> None:
    by_path = {API_CONTRACT['base_path'] + endpoint['path']: set(endpoint['query'])
               for endpoint in API_CONTRACT['endpoints']}
    for route in contract.route_table():
        assert set(route['query']) == by_path[route['path']], route['path']
    # Revision 8.1 declared the request-side cursor, so no route needs an
    # out-of-contract extension any more.
    assert not hasattr(contract, 'QUERY_EXTENSION')


def test_limits_bind_and_security_flags_match_the_frozen_contract() -> None:
    assert contract.DEFAULT_BIND == API_CONTRACT['bind']['default']
    assert list(contract.ALLOWED_BINDS) == API_CONTRACT['bind']['allowed']
    assert API_CONTRACT['bind']['public_bind'] is False
    assert list(contract.HOST_ALLOWLIST) == API_CONTRACT['bind']['host_header_allowlist']
    for key, value in API_CONTRACT['limits'].items():
        assert contract.LIMITS[key] == value, key
    security_flags = API_CONTRACT['security']
    assert list(contract.METHOD_ALLOWLIST) == security_flags['method_allowlist']
    assert security_flags['loopback_only'] is True
    assert security_flags['allow_origin_wildcard'] is False
    assert security_flags['cache_control'] == 'no-store'
    assert security_flags['secrets_in_url'] is False
    assert security_flags['model_generation_endpoint'] is False
    assert security_flags['key_or_credential_configuration_endpoint'] is False
    assert API_CONTRACT['x-write-endpoints'] == []


def test_no_write_prepare_payment_upload_or_model_route_exists() -> None:
    paths = ' '.join(route['path'] for route in contract.route_table()).lower()
    for token in ('execute', 'prepare', 'action', 'payment', 'pay', 'submit', 'upload',
                  'withdraw', 'cancel', 'renew', 'book', 'reserve', 'model', 'generate',
                  'key', 'credential', 'config', 'secret', 'admin', 'proxy', 'forward',
                  'download', 'file', 'raw'):
        assert token not in paths, token
    assert set(contract.METHOD_ALLOWLIST) == {'GET', 'HEAD'}


def test_describe_api_is_json_serialisable_and_lists_every_sample() -> None:
    description = local_api.describe_api()
    json.dumps(description, ensure_ascii=False)
    assert description['contract'] == 'local-read-api-v1'
    assert description['facade_contract'] == 'business-facade-v1'
    assert len(description['routes']) == 7
    assert set(description['display_states']) >= {'READY', 'EMPTY', 'PARTIAL',
                                                  'LOGIN_REQUIRED', 'LIMITED', 'UNKNOWN'}
    assert description['samples']
    assert 'query_extension' not in description


@pytest.mark.parametrize('name', sorted(contract.ERROR_CATEGORIES))
def test_local_error_codes_are_registered_with_the_shared_contract(name) -> None:
    assert source_state.ERROR_CODES[name] == contract.ERROR_CATEGORIES[name]


def test_local_error_codes_do_not_shadow_existing_categories() -> None:
    frozen = json.loads((CONTRACTS / 'error-classification.json').read_text(encoding='utf-8'))
    for name, category in contract.ERROR_CATEGORIES.items():
        assert frozen['codes'][name] == category, name


# ---------------------------------------------------------------------------
# Bind and startup bounds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('bind', ['0.0.0.0', '::', '192.0.2.1', 'localhost', 'example.com'])
def test_public_or_unlisted_bind_is_refused_before_a_socket_opens(tmp_path, bind) -> None:
    with pytest.raises(local_api.LocalApiError) as error:
        local_api.LocalApiServer(FixtureFacade(), bind=bind, state_dir=tmp_path)
    assert error.value.code == 'LOCAL_API_PUBLIC_BIND_REFUSED'


@pytest.mark.parametrize('port', [-1, 65536, '8765', True])
def test_invalid_port_is_refused(tmp_path, port) -> None:
    with pytest.raises(local_api.LocalApiError) as error:
        local_api.LocalApiServer(FixtureFacade(), port=port, state_dir=tmp_path)
    assert error.value.code == 'LOCAL_API_PORT_INVALID'


def test_default_server_binds_loopback_and_an_ephemeral_port(tmp_path) -> None:
    instance = make_server(state_dir=tmp_path)
    try:
        assert instance.bind == '127.0.0.1'
        assert instance.port and instance.port > 0
        assert instance.base_url == f'http://127.0.0.1:{instance.port}'
    finally:
        instance.stop()


def test_start_failure_is_a_stable_error_not_a_traceback(tmp_path) -> None:
    first = make_server(state_dir=tmp_path / 'first')
    try:
        with pytest.raises(local_api.LocalApiError) as busy:
            local_api.LocalApiServer(FixtureFacade(), port=first.port,
                                     state_dir=tmp_path / 'other').start()
        assert busy.value.code == 'LOCAL_API_START_FAILED'
        assert 'Traceback' not in str(busy.value)
    finally:
        first.stop()


# ---------------------------------------------------------------------------
# Host, Origin and method boundary
# ---------------------------------------------------------------------------

def test_unknown_host_header_is_refused_without_reaching_the_facade(server, facade, client) -> None:
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/health', headers=auth_headers(client),
        host_header='evil.example')
    assert status == 403
    assert payload['error']['code'] == 'LOCAL_API_HOST_NOT_ALLOWED'
    assert payload['request_id']
    assert facade.calls == []


def test_host_header_with_the_wrong_port_is_refused(server, facade, client) -> None:
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/health', headers=auth_headers(client),
        host_header=f'127.0.0.1:{server.port + 1}')
    assert status == 403
    assert payload['error']['code'] == 'LOCAL_API_HOST_NOT_ALLOWED'
    assert facade.calls == []


def test_localhost_alias_is_accepted(server, client) -> None:
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/health', headers=auth_headers(client),
        host_header=f'localhost:{server.port}')
    assert status == 200
    assert payload['status'] == 'OK'


@pytest.mark.parametrize('origin', ['https://evil.example', 'null', 'http://127.0.0.1:1',
                                    'http://evil.example:PORT'])
def test_cross_site_or_unknown_origin_is_refused(server, facade, client, origin) -> None:
    origin = origin.replace('PORT', str(server.port))
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/health',
        headers={**auth_headers(client), 'Origin': origin})
    assert status == 403
    assert payload['error']['code'] == 'LOCAL_API_ORIGIN_NOT_ALLOWED'
    assert facade.calls == []


def test_exact_same_origin_is_accepted(server, client) -> None:
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/health',
        headers={**auth_headers(client), 'Origin': f'http://127.0.0.1:{server.port}'})
    assert status == 200


@pytest.mark.parametrize('site', ['cross-site', 'same-site'])
def test_cross_site_fetch_metadata_is_refused(server, facade, client, site) -> None:
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/health',
        headers={**auth_headers(client), 'Sec-Fetch-Site': site})
    assert status == 403
    assert facade.calls == []


def test_no_cors_wildcard_header_is_ever_emitted(server, client) -> None:
    _status, headers, _payload = raw_request(
        server, 'GET', '/api/v1/health', headers=auth_headers(client))
    assert 'Access-Control-Allow-Origin' not in headers
    assert 'Access-Control-Allow-Credentials' not in headers


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE'])
def test_non_read_method_is_refused_before_the_facade(server, facade, client, method) -> None:
    status, headers, payload = json_request(
        server, method, '/api/v1/health', headers=auth_headers(client))
    assert status == 405
    assert headers['Allow'] == 'GET, HEAD'
    assert payload['error']['code'] == 'LOCAL_API_METHOD_NOT_ALLOWED'
    assert facade.calls == []


def test_get_body_is_refused_and_oversized_body_is_413(server, client) -> None:
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/health', headers=auth_headers(client), body=b'x' * 10)
    assert status == 400
    assert payload['error']['code'] == 'LOCAL_API_REQUEST_BODY_NOT_ALLOWED'
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/health', headers=auth_headers(client),
        body=b'x' * (contract.LIMITS['request_body_bytes'] + 1))
    assert status == 413
    assert payload['error']['code'] == 'LOCAL_API_REQUEST_TOO_LARGE'


def test_unknown_route_and_prepare_route_are_not_proxied(server, facade, client) -> None:
    for path in ('/api/v1/actions/execute', '/api/v1/prepare', '/api/v1/query/learn/courses/extra',
                 '/api/v1/unknown', '/api/v1'):
        status, _headers, payload = json_request(
            server, 'GET', path, headers=auth_headers(client))
        assert status == 404, path
        assert payload['error']['code'] == 'LOCAL_API_ROUTE_NOT_FOUND', path
    assert facade.calls == []


def test_path_traversal_and_backslashes_are_rejected(server, facade, client) -> None:
    for path in ('/api/v1/query/learn/..%2f..%2fetc/passwd', '/api/v1/%2e%2e/secret',
                 '/assets/../bootstrap.json', '/api/v1/query/learn/%5c..%5cwindows'):
        status, _headers, payload = json_request(
            server, 'GET', path, headers=auth_headers(client))
        assert status == 400, path
        assert payload['error']['code'] in ('LOCAL_API_PATH_INVALID',
                                            'LOCAL_API_ROUTE_NOT_FOUND'), path
    assert facade.calls == []


# ---------------------------------------------------------------------------
# Sessions and bootstrap channel
# ---------------------------------------------------------------------------

def test_api_without_a_session_is_refused_and_does_not_read(server, facade) -> None:
    status, _headers, payload = json_request(server, 'GET', '/api/v1/health')
    assert status == 401
    assert payload['error']['code'] == 'LOCAL_API_SESSION_REQUIRED'
    assert payload['display']['state'] == 'LOCAL_SESSION_REQUIRED'
    assert '学校' in payload['display']['next_step']
    assert facade.calls == []


def test_bootstrap_header_establishes_a_cookie_with_local_protection(server, facade) -> None:
    record = json.loads(server.bootstrap_file.read_text(encoding='utf-8'))
    status, headers, payload = json_request(
        server, 'GET', '/api/v1/health',
        headers={'X-Openthu-Bootstrap': record['token']})
    assert status == 200
    cookie = headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie
    assert 'Path=/' in cookie and 'Max-Age=' in cookie
    assert 'Domain=' not in cookie
    assert security.COOKIE_NAME in cookie
    assert payload['status'] == 'OK'
    # The token is never echoed in the body or a header.
    assert record['token'] not in json.dumps(payload)
    assert all(record['token'] not in value for value in headers.values())


def test_invalid_and_expired_bootstrap_tokens_are_distinguished(tmp_path, facade) -> None:
    wall = FakeWallClock()
    instance = make_server(facade=facade, state_dir=tmp_path, wall_clock=wall)
    try:
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/health',
            headers={'X-Openthu-Bootstrap': 'synthetic-invalid-token-value'})
        assert status == 401
        assert payload['error']['code'] == 'LOCAL_API_BOOTSTRAP_INVALID'
        record = json.loads(instance.bootstrap_file.read_text(encoding='utf-8'))
        wall.advance(contract.LIMITS['bootstrap_ttl_seconds'] + 1)
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/health',
            headers={'X-Openthu-Bootstrap': record['token']})
        assert status == 401
        assert payload['error']['code'] == 'LOCAL_API_BOOTSTRAP_EXPIRED'
        assert facade.calls == []
    finally:
        instance.stop()


def test_cookie_from_a_stopped_instance_is_not_reused_by_a_new_one(tmp_path, facade) -> None:
    first = make_server(facade=facade, state_dir=tmp_path / 'state-a')
    old_client = LocalApiClient.from_bootstrap(first.bootstrap_file, clock=FakeWallClock())
    old_client.bootstrap()
    old_cookie = old_client.cookie
    facade.calls.clear()
    first.stop()
    second = make_server(facade=facade, state_dir=tmp_path / 'state-b')
    try:
        status, _headers, payload = json_request(
            second, 'GET', '/api/v1/health',
            headers={'Cookie': f'{security.COOKIE_NAME}={old_cookie}'})
        assert status == 401
        assert payload['error']['code'] == 'LOCAL_API_SESSION_INVALID'
        assert facade.calls == []
    finally:
        second.stop()


def test_expired_and_unknown_sessions_stay_distinguishable(tmp_path, facade) -> None:
    clock = FakeClock()
    instance = make_server(facade=facade, state_dir=tmp_path, clock=clock)
    try:
        client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()
        facade.calls.clear()
        clock.advance(contract.LIMITS['session_ttl_seconds'] + 1)
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/health', headers=auth_headers(client))
        assert status == 401
        assert payload['error']['code'] == 'LOCAL_API_SESSION_EXPIRED'
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/health',
            headers={'Cookie': f'{security.COOKIE_NAME}=' + 'a' * 43})
        assert status == 401
        assert payload['error']['code'] == 'LOCAL_API_SESSION_INVALID'
        assert facade.calls == []
    finally:
        instance.stop()


def test_session_store_keeps_no_raw_session_id(server, client) -> None:
    snapshot = json.dumps(server._sessions.snapshot(), default=str)
    assert client.cookie not in snapshot
    assert server.session_count == 1


def test_csrf_helper_rejects_a_missing_or_wrong_token(server, client) -> None:
    csrf = server._sessions.csrf_for(client.cookie)
    assert csrf and len(csrf) == 32
    assert server._sessions.check_csrf(client.cookie, csrf) is True
    assert server._sessions.check_csrf(client.cookie, 'x' * 32) is False
    assert server._sessions.check_csrf(client.cookie, None) is False


def test_bootstrap_file_is_owner_only_and_holds_no_account_data(tmp_path) -> None:
    instance = make_server(state_dir=tmp_path)
    try:
        info = instance.bootstrap_file.stat()
        if os.name == 'nt':
            assert secure_keys._windows_acl_owner_only(instance.bootstrap_file.parent) is True
            assert secure_keys._windows_acl_owner_only(instance.bootstrap_file) is True
        else:
            assert info.st_mode & 0o777 == 0o600
        record = json.loads(instance.bootstrap_file.read_text(encoding='utf-8'))
        assert record['schema'] == security.BOOTSTRAP_SCHEMA
        assert set(record) == {'schema', 'instance_id', 'port', 'base_url', 'token', 'pid',
                               'created_at', 'expires_at', 'api_version'}
        assert 'account' not in record and 'session' not in record and 'cookie' not in record
        assert security.read_bootstrap(instance.bootstrap_file, clock=FakeWallClock())['token'] \
            == record['token']
    finally:
        instance.stop()


def test_bootstrap_file_is_removed_on_stop_and_never_rewritten_by_another_instance(tmp_path) -> None:
    first = make_server(state_dir=tmp_path)
    second = make_server(state_dir=tmp_path)
    try:
        first_path, second_path = first.bootstrap_file, second.bootstrap_file
        assert first_path != second_path
        first.stop()
        assert not first_path.exists()
        assert second_path.exists()
        status, _headers, payload = json_request(
            second, 'GET', '/api/v1/health',
            headers={'X-Openthu-Bootstrap': json.loads(
                second_path.read_text(encoding='utf-8'))['token']})
        assert status == 200
    finally:
        first.stop()
        second.stop()


def test_bootstrap_file_with_loose_permissions_is_rejected(tmp_path) -> None:
    if os.name == 'nt':
        pytest.skip('POSIX permission bits')
    path = tmp_path / 'bootstrap.json'
    path.write_text(json.dumps({
        'schema': security.BOOTSTRAP_SCHEMA, 'instance_id': 'a' * 32, 'port': 1234,
        'base_url': 'http://127.0.0.1:1234', 'token': 't' * 43, 'pid': 1,
        'created_at': '2026-11-17T00:00:00+00:00',
        'expires_at': '2026-11-18T00:00:00+00:00', 'api_version': 'local-read-api-v1'}), encoding='utf-8')
    path.chmod(0o644)
    with pytest.raises(local_api.LocalApiError) as error:
        security.read_bootstrap(path, clock=FakeWallClock())
    assert error.value.code == 'LOCAL_API_BOOTSTRAP_INVALID'


@pytest.mark.skipif(os.name != 'nt', reason='Windows ACL only')
def test_windows_bootstrap_hardens_custom_state_dir_with_broad_parent_acl(tmp_path) -> None:
    custom = tmp_path / 'custom-state'
    custom.mkdir()
    result = subprocess.run(
        ['icacls', str(custom), '/grant', '*S-1-1-0:(OI)(CI)F'],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    instance = make_server(state_dir=custom)
    try:
        assert secure_keys._windows_acl_owner_only(instance.bootstrap_file.parent) is True
        assert secure_keys._windows_acl_owner_only(instance.bootstrap_file) is True
        _, sddl = secure_keys._windows_acl_snapshot(instance.bootstrap_file)
        assert 'WD' not in secure_keys._sddl_ace_trustees(sddl)
        client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()
        status, payload = client.get('/api/v1/health')
        assert status == 200
        assert payload['status'] == 'OK'
    finally:
        instance.stop()


@pytest.mark.skipif(os.name != 'nt', reason='Windows ACL only')
def test_windows_bootstrap_publish_fails_closed_on_preexisting_broad_directory(tmp_path) -> None:
    bootstrap_dir = tmp_path / 'local-api'
    bootstrap_dir.mkdir()
    result = subprocess.run(
        ['icacls', str(bootstrap_dir), '/grant', '*S-1-1-0:F'],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    channel = security.BootstrapChannel(tmp_path, instance_id='a' * 32)
    with pytest.raises(local_api.LocalApiError) as error:
        channel.publish(port=12345, base_url='http://127.0.0.1:12345', pid=os.getpid())
    assert error.value.code == 'LOCAL_API_BOOTSTRAP_UNAVAILABLE'
    assert not list(bootstrap_dir.iterdir())

    server_instance = local_api.LocalApiServer(facade=FixtureFacade(), state_dir=tmp_path)
    with pytest.raises(local_api.LocalApiError) as server_error:
        server_instance.start()
    assert server_error.value.code == 'LOCAL_API_START_FAILED'
    assert not list(bootstrap_dir.iterdir())


@pytest.mark.skipif(os.name != 'nt', reason='Windows ACL only')
def test_windows_bootstrap_read_rejects_broad_file_and_broad_parent_acl(tmp_path) -> None:
    first = make_server(state_dir=tmp_path / 'state-file-broad')
    try:
        result = subprocess.run(
            ['icacls', str(first.bootstrap_file), '/grant', '*S-1-1-0:F'],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        with pytest.raises(local_api.LocalApiError) as err:
            security.read_bootstrap(first.bootstrap_file, clock=FakeWallClock())
        assert err.value.code == 'LOCAL_API_BOOTSTRAP_INVALID'
    finally:
        first.stop()

    second = make_server(state_dir=tmp_path / 'state-dir-broad')
    try:
        result = subprocess.run(
            ['icacls', str(second.bootstrap_file.parent), '/grant', '*S-1-1-0:F'],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        with pytest.raises(local_api.LocalApiError) as err:
            security.read_bootstrap(second.bootstrap_file, clock=FakeWallClock())
        assert err.value.code == 'LOCAL_API_BOOTSTRAP_INVALID'
    finally:
        second.stop()


@pytest.mark.skipif(os.name != 'nt', reason='Windows ACL only')
def test_windows_bootstrap_fails_closed_when_acl_cannot_be_verified(tmp_path, monkeypatch) -> None:
    existing = make_server(state_dir=tmp_path / 'existing')
    try:
        def fail_snapshot(_path):
            raise SecureKeyPermissionError('acl-snapshot-query')

        monkeypatch.setattr(secure_keys, '_windows_acl_snapshot', fail_snapshot)
        with pytest.raises(local_api.LocalApiError) as read_err:
            security.read_bootstrap(existing.bootstrap_file, clock=FakeWallClock())
        assert read_err.value.code == 'LOCAL_API_BOOTSTRAP_INVALID'

        blocked = tmp_path / 'blocked'
        channel = security.BootstrapChannel(blocked, instance_id='b' * 32)
        with pytest.raises(local_api.LocalApiError) as pub_err:
            channel.publish(port=12345, base_url='http://127.0.0.1:12345', pid=os.getpid())
        assert pub_err.value.code == 'LOCAL_API_BOOTSTRAP_UNAVAILABLE'
        assert not channel.path.exists()
        assert not list((blocked / 'local-api').glob('*.tmp'))
    finally:
        existing.stop()


def test_simulated_windows_publish_fails_before_writing_token_when_temp_acl_unverified(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(security, 'os', SimpleNamespace(**{**vars(os), 'name': 'nt'}))
    monkeypatch.setattr(secure_keys, '_harden_windows_acl', lambda path, *, directory=False: None)
    monkeypatch.setattr(secure_keys, '_windows_acl_owner_only', lambda path: Path(path).name == 'local-api')
    channel = security.BootstrapChannel(tmp_path, instance_id='c' * 32)
    with pytest.raises(local_api.LocalApiError) as err:
        channel.publish(port=12345, base_url='http://127.0.0.1:12345', pid=os.getpid())
    assert err.value.code == 'LOCAL_API_BOOTSTRAP_UNAVAILABLE'
    assert not channel.path.exists()
    assert not list((tmp_path / 'local-api').iterdir())


def test_simulated_windows_read_rejects_unverified_parent_or_file_acl(tmp_path, monkeypatch) -> None:
    bootstrap_dir = tmp_path / 'local-api'
    bootstrap_dir.mkdir()
    target = bootstrap_dir / 'bootstrap-d.json'
    target.write_text(json.dumps({
        'schema': security.BOOTSTRAP_SCHEMA,
        'instance_id': 'd' * 32,
        'port': 1234,
        'base_url': 'http://127.0.0.1:1234',
        'token': 't' * 43,
        'pid': 1,
        'created_at': '2026-11-17T00:00:00+00:00',
        'expires_at': '2026-11-18T00:00:00+00:00',
        'api_version': 'local-read-api-v1',
    }), encoding='utf-8')

    monkeypatch.setattr(security, 'os', SimpleNamespace(**{**vars(os), 'name': 'nt'}))
    monkeypatch.setattr(secure_keys, '_windows_acl_owner_only', lambda path: Path(path) != target)
    with pytest.raises(local_api.LocalApiError) as err_file:
        security.read_bootstrap(target, clock=FakeWallClock())
    assert err_file.value.code == 'LOCAL_API_BOOTSTRAP_INVALID'

    monkeypatch.setattr(secure_keys, '_windows_acl_owner_only', lambda path: Path(path) != bootstrap_dir)
    with pytest.raises(local_api.LocalApiError) as err_dir:
        security.read_bootstrap(target, clock=FakeWallClock())
    assert err_dir.value.code == 'LOCAL_API_BOOTSTRAP_INVALID'

    monkeypatch.setattr(secure_keys, '_windows_acl_owner_only', lambda path: True)
    record = security.read_bootstrap(target, clock=FakeWallClock())
    assert record['token'] == 't' * 43


def test_access_log_never_contains_tokens_or_query_values(tmp_path, caplog) -> None:
    instance = make_server(state_dir=tmp_path)
    try:
        record = json.loads(instance.bootstrap_file.read_text(encoding='utf-8'))
        token = record['token']
        client = LocalApiClient(instance.base_url, token=token, timeout=5)
        client.bootstrap()
        caplog.set_level('INFO', logger='openthu.local_api')
        status, _payload = client.get('/api/v1/query/learn/courses',
                                      {'parameters': json.dumps({'query': 'PRIVATE-SEARCH-TERM'})})
        assert status == 200
        logged = '\n'.join(record.getMessage() for record in caplog.records)
        assert token not in logged
        assert 'PRIVATE-SEARCH-TERM' not in logged
        assert client.cookie not in logged
        assert all('PRIVATE-SEARCH-TERM' not in json.dumps(entry) for entry in instance.access_log)
    finally:
        instance.stop()


# ---------------------------------------------------------------------------
# Query parameters and paging
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('page_size,code', [('0', 'INVALID_ARGUMENT_RANGE'),
                                            ('201', 'INVALID_ARGUMENT_RANGE'),
                                            ('abc', 'INVALID_ARGUMENT_TYPE')])
def test_page_size_bounds_are_enforced(server, facade, client, page_size, code) -> None:
    status, _headers, payload = json_request(
        server, 'GET', f'/api/v1/query/learn/courses?page_size={page_size}',
        headers=auth_headers(client))
    assert status == 400
    assert payload['error']['code'] == code
    assert facade.calls == []


def test_unknown_and_duplicate_query_parameters_are_rejected(server, facade, client) -> None:
    for suffix in ('?token=synthetic', '?url=https%3A%2F%2Fevil.example',
                   '?parameters=%7B%7D&parameters=%7B%7D'):
        status, _headers, payload = json_request(
            server, 'GET', '/api/v1/query/learn/courses' + suffix,
            headers=auth_headers(client))
        assert status == 400, suffix
        assert payload['error']['code'] == 'LOCAL_API_QUERY_INVALID', suffix
    assert facade.calls == []


def test_parameters_must_be_a_bounded_json_object_without_network_keys(server, facade, client) -> None:
    cases = [
        ('not-json', 'LOCAL_API_QUERY_INVALID'),
        ('[]', 'LOCAL_API_QUERY_INVALID'),
        (json.dumps({'url': 'https://evil.example'}), 'LOCAL_API_PARAMETER_FORBIDDEN'),
        (json.dumps({'method': 'POST'}), 'LOCAL_API_PARAMETER_FORBIDDEN'),
        (json.dumps({'token': 'x'}), 'LOCAL_API_PARAMETER_FORBIDDEN'),
        (json.dumps({'query': 'x' * 5000}), 'LOCAL_API_PARAMETERS_TOO_LARGE'),
    ]
    for raw, code in cases:
        status, _headers, payload = json_request(
            server, 'GET', '/api/v1/query/learn/courses?' + urlencode({'parameters': raw}),
            headers=auth_headers(client))
        assert status == 400, raw
        assert payload['error']['code'] == code, raw
    assert facade.calls == []


def test_invalid_dates_are_rejected(server, facade, client) -> None:
    for suffix in ('?start_date=2026-13-01', '?start_date=2026-11-20&end_date=2026-11-19',
                   '?end_date=2026-11-32'):
        status, _headers, payload = json_request(
            server, 'GET', '/api/v1/todos' + suffix, headers=auth_headers(client))
        assert status == 400, suffix
        assert payload['error']['code'] in ('INVALID_ARGUMENT_VALUE', 'INVALID_DATE_RANGE')
    assert facade.calls == []


def test_oversized_facade_page_is_sliced_and_resumed(server, client, facade) -> None:
    status, _headers, first = json_request(
        server, 'GET', '/api/v1/todos?page_size=2', headers=auth_headers(client))
    assert status == 200
    assert len(first['items']) == 2
    assert first['next_cursor'].startswith(paging.CURSOR_PREFIX + '.')
    assert first['snapshot_complete'] is False
    status, _headers, second = json_request(
        server, 'GET', '/api/v1/todos?page_size=2&cursor=' + first['next_cursor'],
        headers=auth_headers(client))
    assert status == 200
    assert len(second['items']) == 2
    status, _headers, third = json_request(
        server, 'GET', '/api/v1/todos?page_size=2&cursor=' + second['next_cursor'],
        headers=auth_headers(client))
    assert status == 200
    assert len(third['items']) == 1
    assert third['next_cursor'] is None
    assert third['snapshot_complete'] is True
    ids = [row['event_id'] for row in first['items'] + second['items'] + third['items']]
    assert len(ids) == len(set(ids)) == 5
    assert facade.calls.count('todos') == 1


def test_cursor_reuse_with_different_parameters_is_rejected(server, client) -> None:
    _status, _headers, first = json_request(
        server, 'GET', '/api/v1/todos?page_size=2', headers=auth_headers(client))
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/todos?page_size=3&cursor=' + first['next_cursor'],
        headers=auth_headers(client))
    assert status == 400
    assert payload['error']['code'] == 'CURSOR_QUERY_MISMATCH'


def test_cursor_is_bound_to_the_session(server, client) -> None:
    _status, _headers, first = json_request(
        server, 'GET', '/api/v1/todos?page_size=2', headers=auth_headers(client))
    second_sid = server._sessions.issue()
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/todos?page_size=2&cursor=' + first['next_cursor'],
        headers={'Cookie': f'{security.COOKIE_NAME}={second_sid}'})
    assert status == 400
    assert payload['error']['code'] == 'CURSOR_QUERY_MISMATCH'


def test_cursor_expires_after_the_frozen_ttl(tmp_path, facade) -> None:
    clock = FakeClock()
    instance = make_server(facade=facade, state_dir=tmp_path, clock=clock)
    try:
        client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()
        _status, _headers, first = json_request(
            instance, 'GET', '/api/v1/todos?page_size=2', headers=auth_headers(client))
        clock.advance(contract.LIMITS['cursor_ttl_seconds'] + 1)
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/todos?page_size=2&cursor=' + first['next_cursor'],
            headers=auth_headers(client))
        assert status == 400
        assert payload['error']['code'] == 'CURSOR_EXPIRED'
    finally:
        instance.stop()


def test_trailing_facade_cursor_is_carried_after_local_slices(server, facade, client) -> None:
    envelope = valid_envelope('agenda', 'todos',
                              items=[{'event_id': f'E{index}'} for index in range(5)])
    envelope['next_cursor'] = 'facade-next-page'
    facade.responses['todos'] = envelope
    _status, _headers, first = json_request(
        server, 'GET', '/api/v1/todos?page_size=2', headers=auth_headers(client))
    _status, _headers, second = json_request(
        server, 'GET', '/api/v1/todos?page_size=2&cursor=' + first['next_cursor'],
        headers=auth_headers(client))
    _status, _headers, third = json_request(
        server, 'GET', '/api/v1/todos?page_size=2&cursor=' + second['next_cursor'],
        headers=auth_headers(client))
    assert third['next_cursor'] == 'facade-next-page'
    assert third['complete'] is False
    assert third['display']['completeness']['complete'] is False


# Every route that 8.1 declares a request-side cursor for.  ``/query`` is not
# here because its facade pages natively and the contract always allowed it.
PAGED_ROUTES = (
    ('todos', '/api/v1/todos', 'todos', 'agenda', 'todos', {}),
    ('calendar', '/api/v1/calendar', 'calendar', 'agenda', 'calendar', {}),
    ('resources', '/api/v1/resources', 'resources', 'resources', 'resources',
     {'semester': 'SYNTHETIC-TERM'}),
)


def _set_paged_response(facade, facade_name: str, service: str, operation: str) -> None:
    facade.responses[facade_name] = valid_envelope(
        service, operation,
        items=[{'event_id': f'{facade_name.upper()}{index}'} for index in range(5)])


def _paged_path(path: str, extra: dict, **values) -> str:
    query = dict(extra)
    query.update(values)
    return path + '?' + urlencode(query)


@pytest.mark.parametrize('name,path,facade_name,service,operation,extra', PAGED_ROUTES)
def test_every_paged_route_slices_and_resumes(name, path, facade_name, service, operation,
                                              extra, server, facade, client) -> None:
    _set_paged_response(facade, facade_name, service, operation)
    status, _headers, first = json_request(
        server, 'GET', _paged_path(path, extra, page_size=2), headers=auth_headers(client))
    assert status == 200, name
    assert len(first['items']) == 2
    assert first['next_cursor'].startswith(paging.CURSOR_PREFIX + '.')
    assert first['snapshot_complete'] is False
    status, _headers, second = json_request(
        server, 'GET', _paged_path(path, extra, page_size=2, cursor=first['next_cursor']),
        headers=auth_headers(client))
    assert status == 200 and len(second['items']) == 2
    status, _headers, third = json_request(
        server, 'GET', _paged_path(path, extra, page_size=2, cursor=second['next_cursor']),
        headers=auth_headers(client))
    assert status == 200 and len(third['items']) == 1
    assert third['next_cursor'] is None and third['snapshot_complete'] is True
    ids = [row['event_id'] for row in first['items'] + second['items'] + third['items']]
    assert len(ids) == len(set(ids)) == 5
    assert facade.calls.count(facade_name) == 1


def test_lac1_cursor_is_bound_to_the_campus_account(server, facade, client) -> None:
    _set_paged_response(facade, 'todos', 'agenda', 'todos')
    status, _headers, first = json_request(
        server, 'GET', '/api/v1/todos?page_size=2', headers=auth_headers(client))
    assert status == 200 and first['next_cursor'].startswith(paging.CURSOR_PREFIX + '.')
    facade.account = 'other-campus-account'
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/todos?page_size=2&cursor=' + first['next_cursor'],
        headers=auth_headers(client))
    assert status == 400
    assert payload['error']['code'] == 'CURSOR_QUERY_MISMATCH'


def test_paging_binding_includes_the_account_scope() -> None:
    same = paging.binding('todos', {}, {}, 2, 'session', account_scope='acct_one')
    assert same == paging.binding('todos', {}, {}, 2, 'session', account_scope='acct_one')
    assert same != paging.binding('todos', {}, {}, 2, 'session', account_scope='acct_two')
    assert same != paging.binding('todos', {}, {}, 2, 'session', account_scope=None)


@pytest.mark.parametrize('name,path,facade_name,service,operation,extra', PAGED_ROUTES)
def test_cursor_reuse_with_other_parameters_is_rejected_on_every_paged_route(
        name, path, facade_name, service, operation, extra, server, facade, client) -> None:
    _set_paged_response(facade, facade_name, service, operation)
    _status, _headers, first = json_request(
        server, 'GET', _paged_path(path, extra, page_size=2), headers=auth_headers(client))
    if name == 'resources':
        changed = _paged_path(path, extra, page_size=2, cursor=first['next_cursor'],
                              semester='OTHER-TERM')
    else:
        changed = _paged_path(path, extra, page_size=3, cursor=first['next_cursor'])
    status, _headers, payload = json_request(server, 'GET', changed, headers=auth_headers(client))
    assert status == 400, name
    assert payload['error']['code'] == 'CURSOR_QUERY_MISMATCH'


@pytest.mark.parametrize('name,path,facade_name,service,operation,extra', PAGED_ROUTES)
def test_cursor_is_bound_to_the_session_on_every_paged_route(
        name, path, facade_name, service, operation, extra, server, facade, client) -> None:
    _set_paged_response(facade, facade_name, service, operation)
    _status, _headers, first = json_request(
        server, 'GET', _paged_path(path, extra, page_size=2), headers=auth_headers(client))
    second_sid = server._sessions.issue()
    status, _headers, payload = json_request(
        server, 'GET', _paged_path(path, extra, page_size=2, cursor=first['next_cursor']),
        headers={'Cookie': f'{security.COOKIE_NAME}={second_sid}'})
    assert status == 400, name
    assert payload['error']['code'] == 'CURSOR_QUERY_MISMATCH'


@pytest.mark.parametrize('name,path,facade_name,service,operation,extra', PAGED_ROUTES)
def test_cursor_expires_after_the_frozen_ttl_on_every_paged_route(
        name, path, facade_name, service, operation, extra, tmp_path) -> None:
    clock = FakeClock()
    facade = FixtureFacade()
    _set_paged_response(facade, facade_name, service, operation)
    instance = make_server(facade=facade, state_dir=tmp_path, clock=clock)
    try:
        client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()
        _status, _headers, first = json_request(
            instance, 'GET', _paged_path(path, extra, page_size=2), headers=auth_headers(client))
        clock.advance(contract.LIMITS['cursor_ttl_seconds'] + 1)
        status, _headers, payload = json_request(
            instance, 'GET', _paged_path(path, extra, page_size=2, cursor=first['next_cursor']),
            headers=auth_headers(client))
        assert status == 400, name
        assert payload['error']['code'] == 'CURSOR_EXPIRED'
    finally:
        instance.stop()


@pytest.mark.parametrize('name,path,facade_name,service,operation,extra',
                         [row for row in PAGED_ROUTES if row[0] in ('todos', 'calendar')])
def test_a_cursor_this_route_never_issued_is_refused(
        name, path, facade_name, service, operation, extra, server, facade, client) -> None:
    for value in ('not-a-page-store-cursor', 'c1.deadbeef', 'lac1.'):
        status, _headers, payload = json_request(
            server, 'GET', _paged_path(path, extra, page_size=2, cursor=value),
            headers=auth_headers(client))
        assert status == 400, (name, value)
        assert payload['error']['code'] == 'CURSOR_EXPIRED', (name, value)
    assert facade.calls == []


def test_a_cursor_from_another_route_is_refused(server, facade, client) -> None:
    _set_paged_response(facade, 'calendar', 'agenda', 'calendar')
    _set_paged_response(facade, 'todos', 'agenda', 'todos')
    _status, _headers, calendar_first = json_request(
        server, 'GET', '/api/v1/calendar?page_size=2', headers=auth_headers(client))
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/todos?page_size=2&cursor=' + calendar_first['next_cursor'],
        headers=auth_headers(client))
    assert status == 400
    assert payload['error']['code'] == 'CURSOR_QUERY_MISMATCH'


def test_facade_native_paging_is_passed_through(server, facade, client) -> None:
    envelope = valid_envelope('learn', 'courses', items=[{'course_id': 'ONE'}])
    envelope['next_cursor'] = 'facade-cursor'
    facade.responses['query'] = envelope
    _status, _headers, payload = json_request(
        server, 'GET', '/api/v1/query/learn/courses?page_size=50',
        headers=auth_headers(client))
    assert payload['next_cursor'] == 'facade-cursor'
    assert payload['items'] == [{'course_id': 'ONE'}]
    assert facade.calls.count('query') == 1


# ---------------------------------------------------------------------------
# Responses, projection and display cards
# ---------------------------------------------------------------------------

def test_every_route_returns_a_schema_valid_envelope(server, client) -> None:
    paths = [
        '/api/v1/health',
        '/api/v1/services',
        '/api/v1/services/learn',
        '/api/v1/query/learn/courses?parameters=%7B%7D',
        '/api/v1/todos',
        '/api/v1/calendar',
        '/api/v1/resources',
    ]
    for path in paths:
        status, headers, payload = json_request(server, 'GET', path,
                                                headers=auth_headers(client))
        assert status == 200, path
        validate_envelope(payload)
        assert payload['request_id']
        assert headers['X-Request-Id'] == payload['request_id']
        assert headers['Cache-Control'] == 'no-store'
        assert headers['X-Content-Type-Options'] == 'nosniff'
        assert headers['Referrer-Policy'] == 'no-referrer'
        assert 'Access-Control-Allow-Origin' not in headers
        assert payload['display']['state'] in contract.display_states()
        assert set(payload['display']['completeness']) == {
            'source_complete', 'snapshot_complete', 'complete'}


def test_error_bodies_are_valid_envelopes_with_classified_errors(server, client) -> None:
    cases = [
        ('GET', '/api/v1/unknown', 404),
        ('POST', '/api/v1/health', 405),
        ('GET', '/api/v1/query/learn/courses?page_size=999', 400),
        ('GET', '/api/v1/todos?start_date=bad', 400),
    ]
    for method, path, expected in cases:
        status, _headers, payload = json_request(server, method, path,
                                                 headers=auth_headers(client))
        assert status == expected, path
        validate_envelope(payload)
        assert payload['error']['automatic_replay_allowed'] is False
        assert payload['error']['category'] in {
            'INPUT', 'AUTH', 'PERMISSION', 'RATE_LIMIT', 'MAINTENANCE', 'NETWORK',
            'UPSTREAM_CONTRACT', 'COMPLETENESS', 'CONFLICT', 'NOT_IMPLEMENTED',
            'CANCELLED', 'INTERNAL'}
        assert payload['display']['state'] in contract.display_states()


def test_health_reports_each_service_and_a_display_state(server, client) -> None:
    status, _headers, payload = json_request(server, 'GET', '/api/v1/health',
                                             headers=auth_headers(client))
    assert status == 200
    assert payload['display']['state'] == 'READY'
    assert payload['data']['services'][0]['service_id'] == 'learn'
    assert payload['display']['provenance']['service_id'] == 'local'


def test_services_route_projects_only_display_catalog_fields(server, facade, client) -> None:
    envelope = valid_envelope('local', 'services', items=[
        {'id': 'learn', 'name': '网络学堂', 'category': '学习', 'description': '合成',
         'entry_url': 'https://learn.tsinghua.edu.cn/', 'cookie': 'synthetic'}])
    facade.responses['services'] = envelope
    status, _headers, payload = json_request(server, 'GET', '/api/v1/services',
                                             headers=auth_headers(client))
    assert status == 200
    assert payload['items'] == [{'id': 'learn', 'name': '网络学堂', 'category': '学习',
                                 'description': '合成'}]


def test_service_detail_has_no_local_write_route_and_points_to_controlled_entry(server, client) -> None:
    status, _headers, payload = json_request(server, 'GET', '/api/v1/services/learn',
                                             headers=auth_headers(client))
    assert status == 200
    assert payload['data']['write_routes'] == []
    assert payload['data']['actions']
    assert all(action['executable_via'] == 'MCP_CLI_ONLY'
               and action['local_route'] is None
               for action in payload['data']['actions'])


def test_auth_required_business_card_is_http_200_and_not_retried(server, facade, client) -> None:
    facade.responses['query'] = sample_module.auth_required_sample()
    status, _headers, payload = json_request(
        server, 'GET', query_path('learn', 'assignments'), headers=auth_headers(client))
    assert status == 200
    assert payload['status'] == 'AUTH_REQUIRED'
    assert payload['display']['state'] == 'LOGIN_REQUIRED'
    assert '受控入口' in payload['display']['next_step']
    assert facade.calls == ['query']


def test_sensitive_fields_and_private_paths_never_reach_the_browser(server, facade, client) -> None:
    envelope = valid_envelope('learn', 'courses', items=[{
        'course_id': 'SYNTHETIC-1', 'course_name': '合成课程',
        'cookie': 'synthetic-cookie', 'token': 'synthetic-token',
        'member': '合成成员', 'path': '/home/synthetic/private/notes.txt',
        'note': 'C:\\Users\\example\\Desktop\\answer.txt',
        'session': {'id': 'synthetic'}, 'jwt': 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl',
        'nested': {'authorization': 'Bearer synthetic-secret', 'safe': '保留'},
    }])
    facade.responses['query'] = envelope
    status, _headers, payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert status == 200
    item = payload['items'][0]
    for key in ('cookie', 'token', 'member', 'path', 'session', 'jwt'):
        assert key not in item
    assert 'synthetic-cookie' not in json.dumps(payload)
    assert 'synthetic-token' not in json.dumps(payload)
    assert 'synthetic-secret' not in json.dumps(payload)
    assert '/home/synthetic' not in json.dumps(payload)
    assert 'example\\Desktop' not in json.dumps(payload)
    assert item['nested'] == {'safe': '保留'}
    assert 'LOCAL_API_SENSITIVE_FIELD_DROPPED' in payload['warnings']
    assert 'LOCAL_API_ABSOLUTE_PATH_REDACTED' in payload['warnings']


def test_links_are_limited_to_verified_school_https_sources(server, client) -> None:
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/resources', headers=auth_headers(client))
    assert status == 200
    item = payload['items'][0]
    assert item['official_url'] == 'https://learn.tsinghua.edu.cn/course/1'
    assert item['review_url'] is None
    assert item['entry_url'] is None
    assert 'LOCAL_API_LINK_WITHHELD' in payload['warnings']
    assert payload['display']['provenance']['url'] == 'https://learn.tsinghua.edu.cn/'


def test_unsafe_source_url_is_withheld_in_display(server, facade, client) -> None:
    envelope = valid_envelope('learn', 'courses', items=[])
    envelope['source']['url'] = 'https://evil.example/collect'
    facade.responses['query'] = envelope
    _status, _headers, payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert payload['display']['provenance']['url'] is None
    assert 'LOCAL_API_LINK_WITHHELD' in payload['warnings']


def test_partial_result_is_not_displayed_as_ready(server, facade, client) -> None:
    facade.responses['query'] = sample_module.partial_sample()
    status, _headers, payload = json_request(
        server, 'GET', query_path('library', 'loans'), headers=auth_headers(client))
    assert status == 200
    assert payload['status'] == 'PARTIAL'
    assert payload['display']['state'] == 'PARTIAL'
    assert payload['display']['completeness']['complete'] is False


def test_incomplete_ok_result_is_displayed_as_partial(server, facade, client) -> None:
    envelope = valid_envelope('learn', 'courses', items=[{'course_id': 'X'}],
                              complete=False, source_complete=False)
    envelope['status'] = 'OK'
    facade.responses['query'] = envelope
    _status, _headers, payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert payload['display']['state'] == 'PARTIAL'


def test_head_returns_the_same_metadata_without_a_body(server, client) -> None:
    status, headers, body = raw_request(
        server, 'HEAD', '/api/v1/health', headers=auth_headers(client))
    assert status == 200
    assert body == b''
    assert int(headers['Content-Length']) > 0
    assert headers['Cache-Control'] == 'no-store'
    get_status, _get_headers, get_body = raw_request(
        server, 'GET', '/api/v1/health', headers=auth_headers(client))
    assert get_status == 200
    assert int(headers['Content-Length']) == len(get_body)


# ---------------------------------------------------------------------------
# Facade failures, deadlines and isolation
# ---------------------------------------------------------------------------

def test_facade_exception_becomes_a_safe_502_and_the_server_keeps_serving(server, facade, client) -> None:
    facade.errors['query'] = RuntimeError('synthetic secret /home/synthetic/private')
    status, _headers, payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert status == 502
    assert payload['error']['code'] == 'LOCAL_API_FACADE_UNAVAILABLE'
    assert 'synthetic secret' not in json.dumps(payload)
    assert '/home/synthetic' not in json.dumps(payload)
    assert 'Traceback' not in json.dumps(payload)
    facade.errors.pop('query')
    status, _headers, payload = json_request(server, 'GET', '/api/v1/health',
                                             headers=auth_headers(client))
    assert status == 200


@pytest.mark.parametrize('bad', [
    {'missing': 'everything'},
    {'status': 'SUCCESS', 'source': {'service_id': 'learn', 'operation': 'courses',
                                     'adapter_version': 'x'},
     'checked_at': '2026-11-17T12:00:00+08:00', 'verification_level': 'NONE',
     'source_complete': False, 'complete': False, 'next_cursor': None, 'warnings': []},
    {'status': 'EMPTY', 'source': {'service_id': 'learn', 'operation': 'courses',
                                   'adapter_version': 'x'},
     'checked_at': '2026-11-17T12:00:00+08:00', 'verification_level': 'NONE',
     'source_complete': True, 'complete': False, 'next_cursor': None, 'warnings': []},
])
def test_malformed_facade_envelope_is_502_never_a_fabricated_success(server, facade,
                                                                     client, bad) -> None:
    facade.responses['query'] = bad
    status, _headers, payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert status == 502
    assert payload['error']['code'] == 'LOCAL_API_FACADE_INVALID_RESPONSE'
    assert payload['status'] == 'UNKNOWN'
    validate_envelope(payload)


def test_one_slow_source_times_out_without_blocking_other_cards(tmp_path, facade) -> None:
    limits = contract.LocalApiLimits(ui_wait_seconds=0.2, refresh_min_seconds=0)
    instance = make_server(facade=facade, state_dir=tmp_path, limits=limits)
    try:
        client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()
        facade.delay['todos'] = 1.0
        slow_result = {}

        def slow_call():
            try:
                slow_result['value'] = json_request(
                    instance, 'GET', '/api/v1/todos', headers=auth_headers(client))
            except Exception as error:  # the request may be cut by shutdown
                slow_result['error'] = error

        thread = threading.Thread(target=slow_call, daemon=True)
        started = time.monotonic()
        thread.start()
        while instance.inflight_count == 0 and time.monotonic() - started < 2:
            time.sleep(0.01)
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/health', headers=auth_headers(client))
        assert status == 200
        assert payload['status'] == 'OK'
        thread.join(timeout=3)
        assert not thread.is_alive()
        status, _headers, payload = slow_result['value']
        assert status == 504
        assert payload['error']['code'] == 'LOCAL_API_READ_TIMEOUT'
        validate_envelope(payload)
    finally:
        instance.stop()


def test_stop_cancels_the_requests_this_instance_holds(tmp_path, facade) -> None:
    limits = contract.LocalApiLimits(ui_wait_seconds=5, refresh_min_seconds=0)
    instance = make_server(facade=facade, state_dir=tmp_path, limits=limits)
    client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
    client.bootstrap()
    facade.delay['todos'] = 5.0
    outcome = {}

    def slow_call():
        try:
            outcome['value'] = client.get('/api/v1/todos')
        except Exception as error:
            outcome['error'] = error

    thread = threading.Thread(target=slow_call, daemon=True)
    thread.start()
    started = time.monotonic()
    while instance.inflight_count == 0 and time.monotonic() - started < 3:
        time.sleep(0.01)
    tokens = list(instance._inflight.values())
    assert tokens
    before = time.monotonic()
    instance.stop()
    assert time.monotonic() - before < 2.0
    assert all(token.cancelled for token in tokens)
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_two_instances_are_independent_and_closing_one_keeps_the_other(tmp_path, facade) -> None:
    first = make_server(facade=facade, state_dir=tmp_path / 'a')
    second = make_server(facade=facade, state_dir=tmp_path / 'b')
    second_client = LocalApiClient.from_bootstrap(second.bootstrap_file, clock=FakeWallClock())
    second_client.bootstrap()
    try:
        first.stop()
        status, _headers, payload = json_request(
            second, 'GET', '/api/v1/health', headers=auth_headers(second_client))
        assert status == 200
        assert second.session_count == 1
    finally:
        second.stop()


def test_stop_is_idempotent_and_clears_process_local_state(tmp_path) -> None:
    instance = make_server(state_dir=tmp_path)
    client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
    client.bootstrap()
    instance.stop()
    instance.stop()
    assert instance.session_count == 0
    assert len(instance._pages) == 0


def test_server_starts_without_a_facade_and_reports_the_capability_gap(tmp_path) -> None:
    instance = local_api.LocalApiServer(state_dir=tmp_path)
    instance.start()
    try:
        client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()
        status, payload = client.get('/api/v1/query/learn/courses', {'parameters': '{}'})
        assert status == 200
        assert payload['status'] == 'UNKNOWN'
        assert payload['implementation_state'] == 'BLOCKED'
        assert payload['display']['state'] == 'UNKNOWN'
        assert payload['error']['code'] == 'ADAPTER_REQUIRED'
        assert payload['items'] == []
    finally:
        instance.stop()


# ---------------------------------------------------------------------------
# Static shell, process ownership and privacy
# ---------------------------------------------------------------------------

def test_static_shell_requires_installation_and_serves_assets_safely(tmp_path, facade) -> None:
    without_ui = make_server(facade=facade, state_dir=tmp_path / 'no-ui')
    try:
        client = LocalApiClient.from_bootstrap(without_ui.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()
        status, _headers, payload = json_request(without_ui, 'GET', '/',
                                                 headers={'Cookie': f'{security.COOKIE_NAME}={client.cookie}'})
        assert status == 404
        assert payload['error']['code'] == 'LOCAL_API_UI_NOT_INSTALLED'
    finally:
        without_ui.stop()
    static = tmp_path / 'ui'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('<!doctype html><title>synthetic</title>', encoding='utf-8')
    (static / 'assets' / 'app.css').write_text('body{}', encoding='utf-8')
    with_ui = make_server(facade=facade, state_dir=tmp_path / 'ui-state', static_dir=static)
    try:
        status, headers, body = raw_request(
            with_ui, 'GET', '/', headers={'Sec-Fetch-Site': 'none'})
        assert status == 200
        assert b'synthetic' in body
        assert 'Set-Cookie' in headers
        assert "default-src 'self'" in headers['Content-Security-Policy']
        status, _headers, body = raw_request(with_ui, 'GET', '/assets/app.css')
        assert status == 200 and body == b'body{}'
        status, headers, _payload = json_request(with_ui, 'POST', '/')
        assert status == 405
        assert headers['Allow'] == 'GET, HEAD'
        status, _headers, payload = json_request(with_ui, 'GET', '/assets/missing.css')
        assert status == 404
        status, _headers, payload = json_request(with_ui, 'GET', '/assets/')
        assert status == 400
    finally:
        with_ui.stop()


def test_cross_site_navigation_is_refused_before_the_static_shell(tmp_path, facade) -> None:
    static = tmp_path / 'ui'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('<!doctype html>', encoding='utf-8')
    instance = make_server(facade=facade, state_dir=tmp_path / 'state', static_dir=static)
    try:
        status, headers, payload = json_request(
            instance, 'GET', '/', headers={'Sec-Fetch-Site': 'cross-site'})
        assert status == 403
        assert payload['error']['code'] == 'LOCAL_API_ORIGIN_NOT_ALLOWED'
        assert 'Set-Cookie' not in headers
        assert instance.session_count == 0
    finally:
        instance.stop()


@pytest.mark.skipif(os.name == 'nt', reason='symlink creation needs extra privileges')
def test_static_asset_symlink_escape_is_not_served(tmp_path, facade) -> None:
    static = tmp_path / 'ui'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('<!doctype html>', encoding='utf-8')
    secret = tmp_path / 'secret.css'
    secret.write_text('secret', encoding='utf-8')
    (static / 'assets' / 'escape.css').symlink_to(secret)
    instance = make_server(facade=facade, state_dir=tmp_path / 'state', static_dir=static)
    try:
        status, _headers, payload = json_request(instance, 'GET', '/assets/escape.css')
        assert status == 404
        assert payload['error']['code'] == 'LOCAL_API_ROUTE_NOT_FOUND'
    finally:
        instance.stop()


def test_no_account_record_is_written_to_the_state_directory(tmp_path, facade) -> None:
    state = tmp_path / 'state'
    instance = make_server(facade=facade, state_dir=state)
    try:
        text = instance.bootstrap_file.read_text(encoding='utf-8')
        for marker in ('SYNTHETIC-COURSE', 'Cookie', 'account', '学号'):
            assert marker not in text
        client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()
        client.get('/api/v1/query/learn/courses', {'parameters': '{}'})
        # The single-use token file is gone; the request wrote nothing else.
        files = sorted(path for path in state.rglob('*') if path.is_file())
        assert files == []
    finally:
        instance.stop()


def test_module_does_not_shell_out_signal_or_kill_other_processes() -> None:
    forbidden = ('os.kill', 'os.killpg', 'subprocess', 'psutil', 'terminate()',
                 'signal.', 'popen', 'system(')
    for path in sorted(SOURCE.glob('*.py')):
        text = path.read_text(encoding='utf-8')
        for token in forbidden:
            assert token not in text, f'{path.name} contains {token}'


def test_module_has_no_outbound_network_import() -> None:
    forbidden = ('import requests', 'urllib.request', 'playwright', 'httpx',
                 'socket.create_connection', 'urlopen', 'aiohttp')
    for path in sorted(SOURCE.glob('*.py')):
        text = path.read_text(encoding='utf-8')
        for token in forbidden:
            assert token not in text, f'{path.name} contains {token}'


def test_request_ids_are_unique_and_never_reused(server, client) -> None:
    seen = set()
    for _index in range(4):
        _status, _headers, payload = json_request(server, 'GET', '/api/v1/health',
                                                  headers=auth_headers(client))
        seen.add(payload['request_id'])
    assert len(seen) == 4
    assert all(re.fullmatch(r'[a-f0-9]{32}', value) for value in seen)


# ---------------------------------------------------------------------------
# Rate limits and refresh interval
# ---------------------------------------------------------------------------

def test_parent_test_helper_route_is_not_a_backdoor(server, facade, client) -> None:
    # A route must never accept an arbitrary upstream URL even as a parameter.
    from urllib.parse import urlencode
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/query/campus/page?' + urlencode(
            {'parameters': json.dumps({'url': 'https://learn.tsinghua.edu.cn/'})}),
        headers=auth_headers(client))
    assert status == 400
    assert payload['error']['code'] == 'LOCAL_API_PARAMETER_FORBIDDEN'
    assert facade.calls == []


def test_request_rate_limit_returns_429_with_retry_after(tmp_path, facade) -> None:
    limits = contract.LocalApiLimits(max_requests_per_window=2, request_window_seconds=10,
                                     refresh_min_seconds=0)
    instance = make_server(facade=facade, state_dir=tmp_path, limits=limits)
    try:
        client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()  # first counted request
        status, _headers, _payload = json_request(instance, 'GET', '/api/v1/health',
                                                  headers=auth_headers(client))
        assert status == 200
        status, headers, payload = json_request(instance, 'GET', '/api/v1/health',
                                                headers=auth_headers(client))
        assert status == 429
        assert payload['error']['code'] == 'LOCAL_API_RATE_LIMITED'
        assert payload['error']['retry_after_ms'] >= 1
        assert headers['Retry-After']
        assert payload['display']['state'] == 'LIMITED'
        assert '学校' not in payload['display']['message']
        assert '来源' not in payload['error']['message']
    finally:
        instance.stop()


def test_identical_expensive_refresh_is_rate_limited_but_other_queries_are_not(
        server, facade, client) -> None:
    status, _headers, _payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert status == 200
    status, _headers, payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert status == 429
    assert payload['error']['code'] == 'LOCAL_API_REFRESH_TOO_SOON'
    assert payload['error']['retry_after_ms'] >= 1
    status, _headers, _payload = json_request(
        server, 'GET', query_path('library', 'loans'), headers=auth_headers(client))
    assert status == 200


# ---------------------------------------------------------------------------
# Projection unit checks
# ---------------------------------------------------------------------------

def test_projection_depth_limit_and_non_finite_numbers() -> None:
    deep = {'a': {'b': {'c': {'d': {'e': {'f': {'g': {'h': {'i': 'too deep'}}}}}}}}}
    warnings: set[str] = set()
    projected = projection._walk(deep, depth=0, warnings=warnings)
    assert 'LOCAL_API_DEPTH_LIMIT' in warnings
    assert projection._walk(float('nan'), depth=0, warnings=warnings) is None


def test_safe_link_rules() -> None:
    assert projection.safe_link('https://learn.tsinghua.edu.cn/x')[0] \
        == 'https://learn.tsinghua.edu.cn/x'
    assert projection.safe_link('http://learn.tsinghua.edu.cn/x')[0] is None
    assert projection.safe_link('https://tsinghua.edu.cn.evil.example/x')[0] is None
    assert projection.safe_link('https://evil.example/?next=learn.tsinghua.edu.cn')[0] is None
    assert projection.safe_link('https://learn.tsinghua.edu.cn/cas/login?service=x')[0] is None
    assert projection.safe_link('https://learn.tsinghua.edu.cn/x?ticket=ST-1')[0] is None
    assert projection.safe_link('https://learn.tsinghua.edu.cn/x?ticket=synthetic')[0] is None


def test_sensitive_key_patterns_drop_credentials_without_dropping_ordinary_keys() -> None:
    warnings: set[str] = set()
    value = projection._walk({'auth_state': 'READABLE_AUTH_UNKNOWN', 'token': 'x',
                              'cookie_header': 'synthetic', 'csrf_token_value': 'synthetic',
                              'pathology': 'keep', 'sort_key': 'keep'}, depth=0,
                             warnings=warnings)
    assert value == {'pathology': 'keep', 'sort_key': 'keep'}
    assert 'LOCAL_API_SENSITIVE_FIELD_DROPPED' in warnings


# ---------------------------------------------------------------------------
# Audit regressions (6.2 audit: P1-1..P1-3, P2-1..P2-15)
# ---------------------------------------------------------------------------

def _mutate_envelope(name: str):
    def apply(envelope: dict) -> None:
        if name == 'items_not_array':
            envelope['items'] = {'not': 'a list'}
        elif name == 'verification_level_unknown':
            envelope['verification_level'] = 'MADE_UP'
        elif name == 'snapshot_complete_not_bool':
            envelope['snapshot_complete'] = 'yes'
        elif name == 'implementation_state_bad':
            envelope['implementation_state'] = 'MAYBE'
        elif name == 'adapter_version_too_long':
            envelope['source']['adapter_version'] = 'v' * 200
        elif name == 'warnings_not_strings':
            envelope['warnings'] = [{'not': 'text'}]
        elif name == 'missing_pages_bad':
            envelope['missing_pages'] = [0]
        elif name == 'non_finite':
            envelope['items'] = [{'value': float('nan')}]
        else:  # pragma: no cover - guard for a typo in the parametrisation
            raise AssertionError(name)
    return apply


@pytest.mark.parametrize('name', [
    'items_not_array', 'verification_level_unknown', 'snapshot_complete_not_bool',
    'implementation_state_bad', 'adapter_version_too_long', 'warnings_not_strings',
    'missing_pages_bad', 'non_finite',
])
def test_optional_envelope_fields_are_validated_before_a_200(server, facade, client, name) -> None:
    envelope = valid_envelope('learn', 'courses', items=[])
    _mutate_envelope(name)(envelope)
    facade.responses['query'] = envelope
    status, _headers, payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert status == 502, name
    assert payload['error']['code'] == 'LOCAL_API_FACADE_INVALID_RESPONSE', name
    assert payload['display']['state'] != 'READY'
    validate_envelope(payload)


def test_list_shaped_link_fields_are_filtered_item_by_item(server, facade, client) -> None:
    envelope = valid_envelope('learn', 'courses', items=[{
        'course_id': 'C1',
        'links': ['https://evil.example/phish', 'http://learn.tsinghua.edu.cn/x',
                  'https://learn.tsinghua.edu.cn/ok'],
        'link': ['https://evil.example/two'],
        'entry_url': ['https://evil.example/three'],
        'attachments': [{'name': 'a', 'href': 'https://evil.example/four'}],
        'official_url': 'https://evil.example/five',
    }])
    facade.responses['query'] = envelope
    status, _headers, payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert status == 200
    item = payload['items'][0]
    assert item['links'] == [None, None, 'https://learn.tsinghua.edu.cn/ok']
    assert item['link'] == [None]
    assert item['entry_url'] == [None]
    assert item['attachments'][0]['href'] is None
    assert item['official_url'] is None
    assert 'LOCAL_API_LINK_WITHHELD' in payload['warnings']
    assert any(warning.startswith('LOCAL_API_LINK_WITHHELD_AT:')
               for warning in payload['warnings'])


def test_non_cooperative_adapter_cannot_starve_health_or_stop(tmp_path) -> None:
    class Stubborn(FixtureFacade):
        def __init__(self) -> None:
            super().__init__()
            self.started = 0
            self._started_lock = threading.Lock()

        def todos(self, *, start_date=None, end_date=None, cursor=None,
                  page_size=50, cancel=None):
            with self._started_lock:
                self.started += 1
            time.sleep(2.0)  # deliberately ignores cancel
            return valid_envelope('agenda', 'todos', items=[])

    facade = Stubborn()
    limits = contract.LocalApiLimits(ui_wait_seconds=0.2, refresh_min_seconds=0)
    instance = make_server(facade=facade, state_dir=tmp_path, limits=limits)
    try:
        client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()
        results = []
        threads = [threading.Thread(
            target=lambda index=index: results.append(json_request(
                instance, 'GET', f'/api/v1/todos?page_size={index + 1}',
                headers=auth_headers(client), timeout=10)),
            daemon=True) for index in range(instance._executor_workers)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 3
        while facade.started < instance._executor_workers and time.monotonic() < deadline:
            time.sleep(0.01)
        assert facade.started == instance._executor_workers
        started = time.monotonic()
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/health', headers=auth_headers(client))
        assert status == 200
        assert payload['status'] == 'OK'
        assert time.monotonic() - started < 1.0
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/todos?page_size=99', headers=auth_headers(client))
        assert status == 503
        assert payload['error']['code'] == 'LOCAL_API_BUSY'
        before = time.monotonic()
        instance.stop()
        assert time.monotonic() - before < 2.0
        for thread in threads:
            thread.join(timeout=3)
    finally:
        instance.stop()


@pytest.mark.parametrize('page_size', ['\u00b2', '\u0661\u0662\u0663', '\u4e00'])
def test_page_size_accepts_ascii_digits_only(server, client, page_size) -> None:
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/query/learn/courses?page_size=' + quote(page_size),
        headers=auth_headers(client))
    assert status == 400
    assert payload['error']['code'] == 'INVALID_ARGUMENT_TYPE'


@pytest.mark.parametrize('literal', ['NaN', 'Infinity', '-Infinity'])
def test_json_non_finite_constants_are_rejected_before_the_facade(server, facade,
                                                                  client, literal) -> None:
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/query/learn/courses?' + urlencode(
            {'parameters': '{"n": ' + literal + '}'}), headers=auth_headers(client))
    assert status == 400
    assert payload['error']['code'] == 'LOCAL_API_QUERY_INVALID'
    assert facade.calls == []


def test_failed_read_does_not_consume_the_refresh_slot(server, facade, client) -> None:
    facade.errors['query'] = RuntimeError('synthetic failure')
    first = json_request(server, 'GET', query_path(), headers=auth_headers(client))
    assert first[0] == 502
    facade.errors.pop('query')
    retry = json_request(server, 'GET', query_path(), headers=auth_headers(client))
    assert retry[0] == 200
    assert retry[2]['status'] == 'OK'


def test_head_does_not_consume_the_refresh_slot(server, client) -> None:
    status, _headers, _body = raw_request(
        server, 'HEAD', '/api/v1/query/learn/courses?parameters=%7B%7D',
        headers=auth_headers(client))
    assert status == 200
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/query/learn/courses?parameters=%7B%7D',
        headers=auth_headers(client))
    assert status == 200
    assert payload['status'] == 'OK'


def test_credential_shaped_keys_are_dropped(server, facade, client) -> None:
    facade.responses['query'] = valid_envelope('learn', 'courses', items=[{
        'course_id': 'C1', 'auth': 'synthetic', 'cookie_header': 'synthetic',
        'csrf_token_value': 'synthetic', 'text': 'kept'}])
    status, _headers, payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert status == 200
    assert payload['items'] == [{'course_id': 'C1', 'text': 'kept'}]
    assert 'LOCAL_API_SENSITIVE_FIELD_DROPPED' in payload['warnings']


def test_response_size_ceiling_is_enforced(server, facade, client) -> None:
    envelope = valid_envelope('learn', 'courses', items=[{'course_id': 'C1'}])
    envelope['data'] = {'blob': ['x' * 2000 for _ in range(5000)]}
    facade.responses['query'] = envelope
    status, _headers, body = raw_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert status == 502
    assert len(body) <= contract.LIMITS['max_response_bytes']
    payload = json.loads(body.decode('utf-8'))
    assert payload['error']['code'] == 'LOCAL_API_RESPONSE_TOO_LARGE'


def test_bootstrap_token_is_single_use(tmp_path, facade) -> None:
    instance = make_server(facade=facade, state_dir=tmp_path)
    try:
        record = json.loads(instance.bootstrap_file.read_text(encoding='utf-8'))
        first = json_request(instance, 'GET', '/api/v1/health',
                             headers={'X-Openthu-Bootstrap': record['token']})
        assert first[0] == 200
        assert 'Set-Cookie' in first[1]
        second = json_request(instance, 'GET', '/api/v1/health',
                              headers={'X-Openthu-Bootstrap': record['token']})
        assert second[0] == 401
        assert second[2]['error']['code'] == 'LOCAL_API_BOOTSTRAP_USED'
        assert instance.session_count == 1
        assert not instance.bootstrap_file.exists()
    finally:
        instance.stop()


def test_session_cap_does_not_evict_an_active_browser_session(tmp_path, facade) -> None:
    static = tmp_path / 'ui'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('<!doctype html>', encoding='utf-8')
    limits = contract.LocalApiLimits(max_sessions=1, refresh_min_seconds=0)
    instance = make_server(facade=facade, state_dir=tmp_path / 'state',
                           static_dir=static, limits=limits)
    try:
        status, headers, _body = raw_request(
            instance, 'GET', '/', headers={'Sec-Fetch-Site': 'none'})
        assert status == 200
        cookie = headers['Set-Cookie']
        sid = cookie.split(';', 1)[0].split('=', 1)[1]
        token = json.loads(instance.bootstrap_file.read_text(encoding='utf-8'))['token']
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/health',
            headers={'X-Openthu-Bootstrap': token})
        assert status == 429
        assert payload['error']['code'] == 'LOCAL_API_SESSION_LIMIT'
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/health',
            headers={'Cookie': f'{security.COOKIE_NAME}={sid}'})
        assert status == 200
        assert payload['status'] == 'OK'
    finally:
        instance.stop()


@pytest.mark.skipif(os.name == 'nt', reason='symlink creation needs extra privileges')
def test_bootstrap_symlink_is_rejected(tmp_path, facade) -> None:
    instance = make_server(facade=facade, state_dir=tmp_path)
    try:
        link = tmp_path / 'bootstrap-link.json'
        link.symlink_to(instance.bootstrap_file)
        with pytest.raises(local_api.LocalApiError) as error:
            security.read_bootstrap(link, clock=FakeWallClock())
        assert error.value.code == 'LOCAL_API_BOOTSTRAP_INVALID'
    finally:
        instance.stop()


def test_duplicate_cookie_values_are_all_verified(server, client) -> None:
    valid = f'{security.COOKIE_NAME}={client.cookie}'
    attacker = f'{security.COOKIE_NAME}=attacker-value'
    for header in (valid, f'{attacker}; {valid}', f'{valid}; {attacker}'):
        status, _headers, payload = json_request(
            server, 'GET', '/api/v1/health', headers={'Cookie': header})
        assert status == 200
        assert payload['status'] == 'OK'


def test_envelope_source_must_match_the_requested_route(server, facade, client) -> None:
    facade.responses['query'] = valid_envelope('library', 'loans', items=[])
    status, _headers, payload = json_request(
        server, 'GET', query_path('learn', 'courses'), headers=auth_headers(client))
    assert status == 502
    assert payload['error']['code'] == 'LOCAL_API_FACADE_INVALID_RESPONSE'


def test_call_with_token_never_invokes_a_method_twice() -> None:
    from thu_assistant.local_api import facade as facade_module
    calls = []

    class Flaky:
        def read(self, *, cancel=None):
            calls.append(cancel is not None)
            raise TypeError('cannot cancel the upstream read')

    with pytest.raises(TypeError):
        facade_module.call_with_token(Flaky().read, cancel=object())
    assert calls == [True]


@pytest.mark.parametrize('key', ['endpoint', 'target', 'destination', 'source',
                                 'origin', 'address', 'return_url'])
def test_url_bearing_parameter_keys_are_rejected(server, facade, client, key) -> None:
    status, _headers, payload = json_request(
        server, 'GET', '/api/v1/query/learn/courses?' + urlencode(
            {'parameters': json.dumps({key: 'https://evil.example/x'})}),
        headers=auth_headers(client))
    assert status == 400
    assert payload['error']['code'] == 'LOCAL_API_PARAMETER_FORBIDDEN'
    assert facade.calls == []


def test_shared_stores_tolerate_concurrent_access() -> None:
    clock = FakeClock()
    sessions = security.SessionStore(instance_id='synthetic', clock=clock,
                                     max_sessions=10000)
    limiter = security.RateLimiter(limit=1000, window=10, refresh_min=10, clock=clock)
    pages = paging.PageStore(clock=clock, max_entries=32)
    items = [{'index': index} for index in range(20)]
    envelope = valid_envelope('learn', 'courses', items=items)
    errors = []

    def work(worker: int) -> None:
        try:
            for index in range(40):
                sid = sessions.issue()
                sessions.validate(sid)
                sessions.snapshot()
                limiter.check_request(f'k{worker}')
                limiter.peek_refresh('shared')
                limiter.record_refresh('shared')
                key = f'{worker}-{index}'
                first = pages.start(key, envelope, items, 5)
                if first.get('next_cursor'):
                    pages.resume(first['next_cursor'], key)
        except contract.LocalApiError as error:
            if error.code != 'CURSOR_EXPIRED':
                errors.append(error.code)
        except Exception as error:  # noqa: BLE001 - the test reports any failure
            errors.append(repr(error))

    threads = [threading.Thread(target=work, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []


def test_serve_helper_maps_localhost_and_stops(monkeypatch) -> None:
    events = {}

    class FakeServer:
        def __init__(self, facade, **kwargs):
            events['facade'] = facade
            events['kwargs'] = kwargs
            self.base_url = 'http://127.0.0.1:1'
            self.port = 1
            self.instance_id = 'synthetic'

        def start(self):
            events['started'] = True
            return self

        def serve_forever(self):
            events['served'] = True

        def stop(self):
            events['stopped'] = True

    monkeypatch.setattr(local_api, 'LocalApiServer', FakeServer)
    result = local_api.serve(host='localhost', port=0, facade=object())
    assert events['kwargs']['bind'] == '127.0.0.1'
    assert events['served'] and events['stopped']
    assert result['status'] == 'STOPPED'


def test_a_facade_without_a_method_is_a_capability_gap_not_a_500(tmp_path) -> None:
    class Minimal:
        def health(self, *, service_id=None):
            return {'status': 'OK', 'checked_at': '2026-11-17T12:00:00+08:00',
                    'services': [{'service_id': 'learn', 'status': 'OK',
                                  'checked_at': '2026-11-17T12:00:00+08:00',
                                  'complete': True}], 'warnings': []}

    instance = make_server(facade=Minimal(), state_dir=tmp_path)
    try:
        client = LocalApiClient.from_bootstrap(instance.bootstrap_file, clock=FakeWallClock())
        client.bootstrap()
        status, _headers, payload = json_request(
            instance, 'GET', '/api/v1/services', headers=auth_headers(client))
        assert status == 501
        assert payload['error']['code'] == 'LOCAL_API_FACADE_UNAVAILABLE'
        assert payload['display']['state'] == 'UNKNOWN'
        assert status != 500
    finally:
        instance.stop()


def test_facade_client_refusals_keep_their_code_and_status(server, facade, client) -> None:
    cases = [
        ('FACADE_SERVICE_UNKNOWN', 404),
        ('FACADE_OPERATION_NOT_REGISTERED', 404),
        ('FACADE_QUERY_CANNOT_PROXY', 404),
        ('FACADE_CAPABILITY_UNAVAILABLE', 409),
    ]
    for code, expected in cases:
        facade.errors['query'] = CampusError(code)
        status, _headers, payload = json_request(
            server, 'GET', query_path(), headers=auth_headers(client))
        assert status == expected, code
        assert payload['error']['code'] == code, code
        assert payload['error']['code'] != 'LOCAL_API_FACADE_UNAVAILABLE', code
    facade.errors.pop('query')


def test_capability_gap_keeps_its_not_implemented_category(server, facade, client) -> None:
    facade.errors['query'] = CampusError('FACADE_CAPABILITY_UNAVAILABLE')
    status, _headers, payload = json_request(
        server, 'GET', query_path(), headers=auth_headers(client))
    assert status == 409
    assert payload['error']['code'] == 'FACADE_CAPABILITY_UNAVAILABLE'
    assert payload['error']['category'] == 'NOT_IMPLEMENTED'
    facade.errors.pop('query')


def test_ipv6_loopback_base_url_keeps_its_brackets() -> None:
    client = LocalApiClient('http://[::1]:38413')
    assert client.base_url == 'http://[::1]:38413'
    assert (client.hostname, client.port) == ('::1', 38413)
