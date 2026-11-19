"""Regression tests for the 6.1 audit findings (P1-1 … P2-15).

Each case is named after the finding it closes and asserts the observable
behaviour the audit report asked for.  No campus request, browser, model call or
real account is used; the resource centre case uses a temporary file key and an
empty synthetic index.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_facade import AGENDA, FakeResources, FakeTodos, assistant  # noqa: E402

from thu_assistant import __main__ as cli  # noqa: E402
from thu_assistant import catalog, source_state  # noqa: E402
from thu_assistant.bridge import CampusError  # noqa: E402
from thu_assistant.facade import BusinessFacade, errors as facade_errors  # noqa: E402
from thu_assistant.facade.requests import RequestRegistry  # noqa: E402
from thu_assistant.mcp_stdio import McpServer, RequestRunner, request_key  # noqa: E402
from thu_assistant.resources.api import ResourceCenter  # noqa: E402
from thu_assistant.resources.errors import ResourceInvalidArgument  # noqa: E402
from thu_assistant.secure_store import SecureStore, create_key_file, load_provider  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / 'contracts' / 'v0.2' / 'result-envelope.schema.json').read_text())
LOCAL_API = json.loads((ROOT / 'contracts' / 'v0.2' / 'local-read-api.schema.json').read_text())


class RecordingCentre(ResourceCenter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        return super().search(**kwargs)


def file_keyed_centre(tmp_path):
    key = tmp_path / 'keys' / 'master.key'
    create_key_file(key, 'windows-dpapi' if os.name == 'nt' else 'file-key')
    return RecordingCentre(SecureStore(load_provider(key), tmp_path / 'resources'),
                           'synthetic-audit-student')


def page_bridge(command, **kwargs):
    if command == 'action_identity':
        return {'account_id': 'synthetic-audit-account'}
    if command == 'read':
        return {'status': 'PAGE_READ_VERIFIED', 'httpStatus': 200,
                'url': kwargs.get('url', 'https://info.tsinghua.edu.cn/'),
                'text': '合成页面', 'checked_at': '2026-11-17T09:00:00+08:00',
                'complete': True, 'warnings': [], 'authentication_state': 'AUTHENTICATED'}
    raise CampusError('CAMPUS_GATEWAY_UNAVAILABLE')


# -- P1-1 / P1-5 / P1-8: the real 4.2 provider ------------------------------
def test_p1_1_real_resource_centre_uses_its_own_search_ceiling(monkeypatch, tmp_path):
    centre = file_keyed_centre(tmp_path)
    a = assistant(monkeypatch, tmp_path, resources=centre)
    value = a.facade.query('local', 'resources', {'semester': '2026-2027-1'})
    assert centre.calls and centre.calls[-1]['limit'] == centre.limits.max_search_limit
    assert centre.calls[-1]['offset'] == 0
    # An empty, never-collected index is PARTIAL in 4.2; what matters here is
    # that the call succeeds with the provider's own ceiling and a valid envelope.
    assert value['status'] in ('OK', 'EMPTY', 'PARTIAL')
    assert value['verification_level'] == 'LOCAL_EXECUTION_STATE'
    jsonschema.validate(value, SCHEMA)


def test_p1_5_resources_schema_has_no_ignored_offset_or_limit(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path, resources=FakeResources())
    schema = a.facade.describe_service('local')['operations'][2]['parameters_schema']['properties']
    assert set(schema) == {'semester', 'query', 'course_id'}
    with pytest.raises(CampusError) as caught:
        a.facade.query('local', 'resources', {'semester': '2026-2027-1', 'offset': 50, 'limit': 10})
    assert caught.value.code == 'INVALID_ARGUMENTS'
    with pytest.raises(CampusError) as caught:
        a.facade.query('local', 'resources', {})
    assert caught.value.code == 'MISSING_ARGUMENT'


def test_p1_8_local_results_validate_against_the_frozen_envelope(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path, resources=FakeResources())
    for operation, parameters in (('agenda', {'start_date': '2026-11-17', 'end_date': '2026-11-23'}),
                                  ('agenda_changes', {'start_date': '2026-11-17', 'end_date': '2026-11-23'}),
                                  ('resources', {'semester': '2026-2027-1'})):
        value = a.facade.query('local', operation, parameters)
        jsonschema.validate(value, SCHEMA)
        assert value['verification_level'] in SCHEMA['properties']['verification_level']['enum']


# -- P1-2 / P2-2: the registration table is the read authority --------------
def test_p1_2_campus_page_is_registered_and_unknown_reads_are_refused(monkeypatch, tmp_path):
    from thu_assistant.mcp_stdio import CampusAssistant
    monkeypatch.setenv('THU_ASSISTANT_STATE_DIR', str(tmp_path / 'state'))
    a = CampusAssistant(learning_factory=lambda: None, bridge=page_bridge)
    entry = a.facade.capability('campus', 'page')
    assert entry is not None and entry.state == 'AVAILABLE' and entry.executable
    value = a.facade.query('campus', 'page', {'url': 'https://info.tsinghua.edu.cn/'})
    assert value['status'] in ('PAGE_READ_VERIFIED', 'AUTHENTICATED')
    assert value['facade']['capability_state'] == 'AVAILABLE'
    assert {'owner', 'executable', 'side_effects', 'authorization'} <= set(value['facade'] | entry.public())
    described = a.facade.describe_service('campus')
    assert [(row['operation'], row['facade']['state']) for row in described['operations']] == \
        [('page', 'AVAILABLE')]
    with pytest.raises(CampusError) as caught:
        a.facade.query('campus', 'not_registered', {})
    assert caught.value.code == 'UNREGISTERED_QUERY_OPERATION'


def test_p2_2_url_is_accepted_only_for_the_registered_page_read(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.query('library', 'loans', {'url': 'https://lib.tsinghua.edu.cn/'})
    assert caught.value.code == 'FACADE_QUERY_CANNOT_PROXY'
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.prepare('library', 'renew_loan', {'record_id': '1', 'url': 'https://x/'})
    assert caught.value.code == 'FACADE_QUERY_CANNOT_PROXY'


# -- P1-3: health aggregate -------------------------------------------------
def test_p1_3_health_status_matches_the_row_state(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    expected = {'info': 'OK', 'library': 'PARTIAL', 'sports': 'PARTIAL',
                'learn': 'PARTIAL', 'local': 'PARTIAL', 'academics': 'BLOCKED'}
    for service, status in expected.items():
        report = a.facade.health(service)
        assert report['status'] == status, (service, report['status'], report['services'][0]['state'])
        if status != 'OK':
            assert report['services'][0]['blockers']


# -- P1-4: every described service is describable ---------------------------
def test_p1_4_describe_lists_only_describable_services(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    described = a.facade.describe()['services']
    assert 'academics' in described
    for service in described:
        value = a.facade.describe_service(service)
        assert value['service_id'] == service
        assert value['facade']['live_probe'] is False
    academics = a.facade.describe_service('academics')
    assert academics['catalog_registered'] is False
    assert {row['operation'] for row in academics['operations']} == {'exams', 'timetable', 'grades'}
    assert all(row['facade']['state'] == 'BLOCKED' for row in academics['operations'])


# -- P1-6: no advertised capability without a public entry ------------------
def test_p1_6_local_write_capabilities_have_no_fake_entry(monkeypatch, tmp_path):
    class Media:
        def cancel_task(self, task_id):
            return {'task_id': task_id}

    a = assistant(monkeypatch, tmp_path, resources=FakeResources(), media=Media())
    for operation in ('media_prepare', 'resources_export'):
        entry = a.facade.capability('local', operation)
        assert (entry.state, entry.reason, entry.executable) == \
            ('MISSING', 'NO_PUBLIC_ENTRY', False), operation
        assert entry.entry == 'none'
    assert not hasattr(a.facade, 'export_resources')
    for entry in a.facade.registrations():
        if entry.state == 'AVAILABLE' and entry.kind in ('LOCAL_WRITE', 'MODEL'):
            raise AssertionError(f'{entry.service_id}.{entry.operation} claims an entry it lacks')


# -- P1-7: non-CampusError codes keep their classification ------------------
class _StringKeyError(Exception):
    code = 'RESOURCE_INVALID_ARGUMENT'


def test_p1_7_provider_error_code_survives_the_mcp_and_cli_boundaries(monkeypatch, tmp_path):
    class Broken(FakeResources):
        def search(self, **kwargs):
            raise ResourceInvalidArgument('limit')

    a = assistant(monkeypatch, tmp_path, resources=Broken())
    response = McpServer(a).handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                    'params': {'name': 'query_service',
                                               'arguments': {'service_id': 'local',
                                                             'operation': 'resources',
                                                             'parameters': {'semester': '2026-2027-1'}}}})
    error = response['result']['structuredContent']['error']
    assert (error['code'], error['category'], error['retryable']) == \
        ('RESOURCE_INVALID_ARGUMENT', 'INPUT', False)
    assert error['automatic_replay_allowed'] is False and error['user_action'] == 'FIX_INPUT'

def test_p1_7_cli_prints_the_registered_code_and_category(monkeypatch, tmp_path, capsys):
    class StubAssistant:
        facade = None

        def __init__(self):
            self.browsers = type('Browsers', (), {'close_all': staticmethod(lambda: None)})()

    monkeypatch.setattr(cli, 'CampusAssistant', lambda: StubAssistant())
    monkeypatch.setattr(cli, '_run', lambda operation, args, assistant: (_ for _ in ()).throw(_StringKeyError()))
    assert cli.main(['health']) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload['error']['code'] == 'RESOURCE_INVALID_ARGUMENT'
    assert payload['error']['category'] == 'INPUT'


# -- P2-1: switches are visible in the capability state ---------------------
def test_p2_1_read_and_renewal_switches_change_the_registered_state(monkeypatch, tmp_path):
    monkeypatch.setenv('THU_TODOS', '0')
    a = assistant(monkeypatch, tmp_path)
    agenda = a.facade.capability('local', 'agenda')
    assert (agenda.state, agenda.reason, agenda.executable) == ('DISABLED', 'TODOS_DISABLED', False)
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.query('local', 'agenda', {})
    assert caught.value.code == 'FACADE_CAPABILITY_UNAVAILABLE'
    monkeypatch.delenv('THU_TODOS')

    a = assistant(monkeypatch, tmp_path)
    monkeypatch.setenv('THU_LIBRARY_RENEW', '0')
    a.facade = BusinessFacade(a, env=dict(os.environ))
    renewal = a.facade.capability('library', 'renew_loan')
    assert (renewal.state, renewal.reason, renewal.prepare_allowed) == \
        ('DISABLED', 'LIBRARY_RENEW_DISABLED', False)
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.prepare('library', 'renew_loan', {'record_id': '1'})
    assert caught.value.code == 'LIBRARY_RENEW_DISABLED'


# -- P2-3: injected environment and clock are honoured ----------------------
def test_p2_3_facade_env_and_clock_are_used(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    facade = BusinessFacade(a, env={'THU_TODOS': '0'}, clock=lambda: 1234.5)
    assert facade.capability('local', 'agenda').state == 'DISABLED'
    assert facade.capability('local', 'agenda').reason == 'TODOS_DISABLED'
    request = facade.requests.begin('clock-check')
    facade.requests.start(request)
    facade.requests.finish(request)
    assert request.started_at == 1234.5 and request.finished_at == 1234.5


# -- P2-4: export option validation ----------------------------------------
def test_p2_4_export_agenda_validates_options_before_delegating(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    target = str(tmp_path / 'agenda.ics')
    with pytest.raises(CampusError) as caught:
        a.facade.export_agenda(target, overwrite='yes')
    assert caught.value.code == 'INVALID_ARGUMENT_TYPE'
    with pytest.raises(CampusError) as caught:
        a.facade.export_agenda(target, allow_partial='no')
    assert caught.value.code == 'INVALID_ARGUMENT_TYPE'
    with pytest.raises(CampusError) as caught:
        a.facade.export_agenda(target, format='xlsx')
    assert caught.value.code == 'EXPORT_FORMAT_UNSUPPORTED'
    with pytest.raises(CampusError) as caught:
        a.facade.export_agenda(123)
    assert caught.value.code == 'EXPORT_DESTINATION_REQUIRED'
    assert a.todos.exports == []


# -- P2-5: probe metadata is not overloaded --------------------------------
def test_p2_5_query_results_do_not_reuse_the_static_probe_flag(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    value = a.facade.query('local', 'agenda', {'start_date': '2026-11-17', 'end_date': '2026-11-23'})
    assert value['facade']['probe'] == 'SOURCE_READ'
    assert 'live_probe' not in value['facade']
    assert a.facade.health('local')['live_probe'] is False


# -- P2-6 / P2-7 / P2-8 / P2-9: the request runner -------------------------
class _Server:
    def __init__(self, *, error=False):
        self.calls = 0
        self.error = error
        self.gate = threading.Event()

    def handle(self, request):
        self.calls += 1
        self.gate.wait(5)
        value = {'error': {'code': 'SYNTHETIC_FAILURE'}} if self.error else {'status': 'OK'}
        return {'jsonrpc': '2.0', 'id': request.get('id'),
                'result': {'content': [{'type': 'text', 'text': json.dumps(value)}],
                           'structuredContent': value, 'isError': self.error}}


def runner(server, registry=None, emitted=None):
    emitted = [] if emitted is None else emitted
    return RequestRunner(server, registry or RequestRegistry(), emitted.append), emitted


def test_p2_6_request_limit_error_is_fully_classified(monkeypatch, tmp_path):
    registry = RequestRegistry(max_active=1)
    registry.begin('int:1')
    run, emitted = runner(_Server(), registry)
    response = run.run({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {}},
                       threading.Event(), time.monotonic())
    error = response['result']['structuredContent']['error']
    assert error['code'] == 'FACADE_REQUEST_LIMIT'
    assert {'category', 'retryable', 'automatic_replay_allowed', 'user_action'} <= set(error)
    assert error['category'] == 'RATE_LIMIT' and error['retryable'] is True
    assert emitted == [response]


def test_p2_7_failed_and_cancelled_requests_keep_distinct_states(monkeypatch, tmp_path):
    server, emitted = _Server(error=True), []
    registry = RequestRegistry()
    run = RequestRunner(server, registry, emitted.append)
    run.run({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {}},
            threading.Event(), time.monotonic())
    assert registry.state('int:7') == 'FAILED'

    server, emitted = _Server(), []
    registry = RequestRegistry()
    run = RequestRunner(server, registry, emitted.append)
    cancel = threading.Event()
    thread = threading.Thread(target=run.run, args=({'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call',
                                                     'params': {}}, cancel, time.monotonic()))
    thread.start()
    deadline = time.monotonic() + 5
    while server.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert registry.state('int:8') == 'RUNNING'
    registry.cancel('int:8', 'PROTOCOL_CANCELLED')
    server.gate.set()
    thread.join(5)
    assert registry.state('int:8') == 'CANCELLED'


def test_p2_9_queued_cancel_never_reaches_the_handler(monkeypatch, tmp_path):
    server, emitted = _Server(), []
    registry = RequestRegistry()
    run = RequestRunner(server, registry, emitted.append)
    event = threading.Event()
    event.set()
    response = run.run({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call', 'params': {}},
                       event, time.monotonic())
    assert server.calls == 0
    assert response['result']['isError'] is True
    assert response['result']['structuredContent']['error']['code'] == 'CANCELLED'
    assert registry.state('int:9') == 'CANCELLED'


def test_p2_8_integer_and_string_request_ids_are_distinct(monkeypatch, tmp_path):
    assert request_key(1) != request_key('1')
    registry = RequestRegistry()
    server, emitted = _Server(), []
    run = RequestRunner(server, registry, emitted.append)
    for rid in (1, '1'):
        run.run({'jsonrpc': '2.0', 'id': rid, 'method': 'tools/call', 'params': {}},
                threading.Event(), time.monotonic())
    assert server.calls == 2
    assert registry.state('int:1') == 'FINISHED' and registry.state('str:1') == 'FINISHED'


# -- P2-12: media cleanup only for a live request --------------------------
def test_p2_12_cancel_does_not_touch_unrelated_media_tasks(monkeypatch, tmp_path):
    class Media:
        def __init__(self):
            self.cancelled = []

        def cancel_task(self, task_id):
            self.cancelled.append(task_id)

    media = Media()
    a = assistant(monkeypatch, tmp_path, media=media)
    unknown = a.facade.cancel('no-such-request')
    assert unknown['cancelled'] is False and unknown['media_cleanup'] == 'NOT_APPLICABLE'
    assert media.cancelled == []

    request = a.facade.requests.begin('req-live')
    a.facade.requests.start(request)
    value = a.facade.cancel('req-live')
    assert value['media_cleanup'] == 'CANCELLED' and media.cancelled == ['req-live']
    a.facade.requests.finish(request)


# -- P2-13: module report failures stay visible ----------------------------
def test_p2_13_academic_report_failure_keeps_a_specific_gap(monkeypatch, tmp_path):
    from thu_assistant import academics

    def broken():
        raise RuntimeError('synthetic-private-detail')

    monkeypatch.setattr(academics, 'capability_report', broken)
    a = assistant(monkeypatch, tmp_path)
    entries = {entry.operation: entry for entry in a.facade.registrations('academics')}
    assert set(entries) == {'exams', 'timetable', 'grades'}
    assert all(entry.state == 'MISSING' and entry.reason == 'CAPABILITY_REPORT_FAILED'
               for entry in entries.values())
    described = a.facade.describe()
    assert {'service_id': 'academics', 'stage': 'ACADEMIC_CAPABILITY',
            'code': 'CAPABILITY_REPORT_FAILED'} in described['registration_failures']
    assert 'synthetic-private-detail' not in json.dumps(described)
    assert a.facade.health('academics')['status'] == 'MISSING'


# -- P2-14: catalog refresh is visible without a restart -------------------
def test_p2_14_service_ids_follow_a_catalog_refresh(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    assert 'synthetic-new' not in a.facade.service_ids()
    original = catalog.load_services

    def refreshed():
        rows = original()
        return rows + [{'id': 'synthetic-new', 'name': '合成新服务', 'category': '合成'}]

    monkeypatch.setattr(catalog, 'load_services', refreshed)
    assert 'synthetic-new' in a.facade.service_ids()
    assert 'synthetic-new' in {row['service_id'] for row in a.facade.health()['services']}


# -- P2-15: semester is documented where 6.2 reads it ----------------------
def test_p2_15_resources_semester_is_a_documented_provider_parameter(monkeypatch, tmp_path):
    endpoint = next(item for item in LOCAL_API['endpoints'] if item['path'] == '/resources')
    assert 'semester' in endpoint['query']
    a = assistant(monkeypatch, tmp_path, resources=FakeResources())
    with pytest.raises(CampusError) as caught:
        a.facade.query('local', 'resources', {})
    assert caught.value.code == 'MISSING_ARGUMENT'


# -- P2-11: CLI migration note --------------------------------------------
def test_p2_11_readme_documents_the_new_cli_and_error_shape():
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    for command in ('openthu describe', 'openthu health', 'openthu query',
                    'openthu agenda', 'openthu dashboard'):
        assert command in readme, command
    assert 'command' in readme and 'category' in readme
