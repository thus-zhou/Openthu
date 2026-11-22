"""Regression tests for the local failure envelopes and agenda export pass-through.

The audit found that ``BusinessFacade._local_failure`` called the instance
``_local_read`` unbound, so /resources with a semester and /todos or /calendar
on a source error raised TypeError inside the dashboard.  It also found that
``openthu agenda --export --semester`` accepted the flag but dropped it before
the todo engine.  These tests exercise the facade directly and the loopback
HTTP route with the real facade; they never contact a campus source.
"""
from __future__ import annotations

import http.client
import json
import tempfile
from types import SimpleNamespace

from thu_assistant import local_api
from thu_assistant.bridge import CampusError
from thu_assistant.facade.core import BusinessFacade
from thu_assistant.local_api.client import LocalApiClient
from thu_assistant.mcp_stdio import CampusAssistant


def _facade() -> BusinessFacade:
    return CampusAssistant().facade


def _assert_failure_envelope(value: dict, code: str) -> None:
    assert isinstance(value, dict)
    assert value['status'] == 'ERROR'
    assert value['items'] == []
    assert value['complete'] is False
    assert value['source_complete'] is False
    assert value['error']['code'] == code
    assert value['error']['automatic_replay_allowed'] is False
    assert isinstance(value['facade']['service_domain'], str)


def test_resources_without_a_semester_is_a_refused_input_envelope():
    value = _facade().resources()
    _assert_failure_envelope(value, 'INVALID_ARGUMENT_VALUE')
    assert value['warnings'] == ['SEMESTER_REQUIRED']


def test_resources_without_a_provider_is_a_capability_gap_not_a_type_error():
    value = _facade().resources(semester='2026-2027-1')
    _assert_failure_envelope(value, 'FACADE_CAPABILITY_UNAVAILABLE')
    assert value['source']['service_id'] == 'resources'
    assert value['source']['operation'] == 'resources'


def test_todos_source_failure_is_an_envelope():
    value = _facade().todos(start_date='2026-01-01', end_date='2027-12-31')
    _assert_failure_envelope(value, 'INVALID_DATE_RANGE')
    assert value['source']['operation'] == 'todos'


def test_calendar_source_failure_is_an_envelope():
    value = _facade().calendar(start_date='2026-01-01', end_date='2027-12-31')
    _assert_failure_envelope(value, 'INVALID_DATE_RANGE')
    assert value['source']['operation'] == 'calendar'


def test_services_catalog_failure_is_an_envelope(monkeypatch):
    facade = _facade()

    def failing(**kwargs):
        raise CampusError('INVALID_ARGUMENT_VALUE')

    monkeypatch.setattr('thu_assistant.catalog.list_services', failing)
    value = facade.services()
    _assert_failure_envelope(value, 'INVALID_ARGUMENT_VALUE')
    assert value['source']['operation'] == 'services'


def test_local_api_returns_the_resources_gap_instead_of_a_502():
    facade = _facade()
    with tempfile.TemporaryDirectory() as state:
        server = local_api.LocalApiServer(facade, state_dir=state).start()
        try:
            client = LocalApiClient.from_bootstrap(server.bootstrap_file)
            client.bootstrap()
            connection = http.client.HTTPConnection('127.0.0.1', server.port, timeout=10)
            try:
                connection.request(
                    'GET', '/api/v1/resources?semester=2026-2027-1',
                    headers={'Cookie': f'{local_api.security.COOKIE_NAME}={client.cookie}'})
                response = connection.getresponse()
                body = json.loads(response.read())
            finally:
                connection.close()
        finally:
            server.stop()
    assert response.status == 200
    assert body['status'] == 'ERROR'
    assert body['error']['code'] == 'FACADE_CAPABILITY_UNAVAILABLE'
    assert body['source']['service_id'] == 'resources'
    assert body['items'] == []


def test_cli_export_passes_the_semester_to_the_facade():
    from thu_assistant import __main__ as cli

    captured = {}

    class Facade:
        def export_agenda(self, path, **kwargs):
            captured.update(kwargs)
            return {'status': 'OK'}

    args = SimpleNamespace(export='/absolute/user/calendar.ics',
                           start_date='2026-11-17', end_date='2026-11-23',
                           services=None, semester='2026-2027-1', format='ics',
                           overwrite=False, allow_partial=True)
    cli._run('agenda', args, SimpleNamespace(facade=Facade()))
    assert captured['semester'] == '2026-2027-1'
    assert captured['start_date'] == '2026-11-17'
    assert captured['allow_partial'] is True


def test_facade_export_passes_the_semester_to_the_todo_engine():
    captured = {}

    class Todos:
        def export_agenda(self, path, **kwargs):
            captured.update(kwargs)
            return {'ok': True}

    facade = BusinessFacade(SimpleNamespace(todos=Todos()), env={})
    facade.capability = lambda *args, **kwargs: SimpleNamespace(state='AVAILABLE')
    value = facade.export_agenda('/absolute/user/calendar.ics',
                                 start_date='2026-11-17', end_date='2026-11-23',
                                 semester='2026-2027-1', allow_partial=True)
    assert captured['semester'] == '2026-2027-1'
    assert captured['allow_partial'] is True
    assert value['facade']['operation'] == 'agenda_export'
    assert value['facade']['kind'] == 'LOCAL_WRITE'
    assert value['facade']['capability_state'] == 'AVAILABLE'
