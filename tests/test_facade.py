"""Public business facade (6.1): registration, gates, envelope and cancellation.

Every case uses a synthetic account, synthetic agenda/resources data and fake
actions; no campus request, model call or browser is started.  The facade is the
place where "the module exists" must never be reported as "the capability is
available", so most cases assert a refusal rather than a happy path.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from thu_assistant import source_state
from thu_assistant.bridge import CampusError
from thu_assistant.facade import BusinessFacade, METHODS, VERSION, errors as facade_errors
from thu_assistant.facade.requests import CANCEL_REQUESTED, CANCELLED, FINISHED, QUEUED, RequestRegistry, scope
from thu_assistant.mcp_stdio import TOOLS, CampusAssistant, McpServer

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / 'contracts' / 'v0.2' / 'error-classification.json'


# -- synthetic providers ---------------------------------------------------
AGENDA = {
    'status': 'OK',
    'source': {'service_id': 'agenda', 'operation': 'agenda', 'adapter_version': 'agenda-events-v1'},
    'checked_at': '2026-11-17T09:00:00+08:00',
    'verification_level': 'NONE',
    'complete': True,
    'source_complete': True,
    'next_cursor': None,
    'timezone': 'Asia/Shanghai',
    'window': {'start_date': '2026-11-17', 'end_date': '2026-11-23'},
    'as_of': '2026-11-17T09:00:00+08:00',
    'sources_status': [{'source': 'learn.assignments', 'complete': True, 'status': 'OK'}],
    'scope_notes': [],
    'selection': {'services': None},
    'items': [],
    'calendar_events': [
        {'event_uid': 'ev-1', 'title': '合成作业一', 'status': 'OPEN', 'calendar_valid': True},
        {'event_uid': 'ev-2', 'title': '合成作业二', 'status': 'OPEN', 'calendar_valid': True},
        {'event_uid': 'ev-3', 'title': '合成借阅', 'status': 'OPEN', 'calendar_valid': True},
    ],
    'undated': [],
    'excluded': [],
    'warnings': [],
    'views': {'upcoming': ['ev-1', 'ev-2', 'ev-3'], 'overdue': [], 'undated': [],
              'completed': [], 'by_source': {'learn.assignments': ['ev-1', 'ev-2']}},
    'conflicts': [],
    'pagination': {'total_items': 3, 'returned_items': 3, 'next_cursor': None},
    'background_polling': False,
    'message_delivery': False,
    'delivery_state': 'NOT_DELIVERED',
}


class FakeTodos:
    def __init__(self, agenda=None, changes=None):
        self.agenda_result = copy.deepcopy(agenda if agenda is not None else AGENDA)
        self.changes_result = copy.deepcopy(changes if changes is not None else {
            'status': 'OK', 'baseline_state': 'COMPARABLE', 'complete': True,
            'source_complete': True, 'checked_at': '2026-11-17T09:00:00+08:00',
            'next_cursor': 'evc1.synthetic-change-cursor',
            'changes': [{'change_id': 'c1', 'type': 'ADDED', 'event_uid': 'ev-1'},
                        {'change_id': 'c2', 'type': 'MODIFIED', 'event_uid': 'ev-2'}],
            'counts': {'added': 1, 'modified': 1}, 'warnings': [],
            'delivery_state': 'NOT_DELIVERED', 'cursor_scope': 'PROCESS_LOCAL',
        })
        self.calls = []
        self.exports = []

    def agenda(self, **kwargs):
        self.calls.append(('agenda', kwargs))
        return copy.deepcopy(self.agenda_result)

    def changes(self, cursor=None, **kwargs):
        self.calls.append(('changes', {'cursor': cursor, **kwargs}))
        return copy.deepcopy(self.changes_result)

    def export_agenda(self, path, **kwargs):
        self.calls.append(('export', {'path': path, **kwargs}))
        self.exports.append((path, kwargs))
        return {'status': 'EXPORTED', 'path': str(path), 'format': kwargs.get('format', 'ics'),
                'delivery_state': 'NOT_DELIVERED', 'events': 3}


class FakeResources:
    def __init__(self, items=None, total=None):
        self.items = list(items if items is not None else [
            {'stable_resource_uid': 'r1', 'title': '合成资料一'},
            {'stable_resource_uid': 'r2', 'title': '合成资料二'},
        ])
        self.total = len(self.items) if total is None else total
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return {'format_version': 'resource-search-v1', 'semester': kwargs['semester'],
                'query': kwargs.get('query'), 'status': 'OK', 'items': copy.deepcopy(self.items),
                'total': self.total, 'total_known': True, 'offset': 0, 'limit': 200,
                'snapshot_complete': True, 'source_complete': True,
                'complete': self.total <= len(self.items), 'checked_at': '2026-11-17T09:00:00+08:00',
                'warnings': [], 'source_errors': []}


class FakeStore:
    def __init__(self, records=None):
        self.records = dict(records or {})

    def read(self, operation_id):
        if operation_id not in self.records:
            raise CampusError('ACTION_NOT_FOUND')
        return copy.deepcopy(self.records[operation_id])


class FakeEngine:
    def __init__(self, records=None):
        self.store = FakeStore(records)
        self.calls = []

    def prepare(self, service, operation, parameters):
        self.calls.append(('prepare', service, operation, parameters))
        return {'operation_id': 'a' * 32, 'plan_id': 'a' * 32, 'status': 'PREPARED',
                'service_id': service, 'operation': operation, 'preview': parameters,
                'expires_at': 0, 'missing_fields': [], 'reason': None,
                'adapter_version': 'synthetic-adapter', 'revision': 0}

    def execute(self, plan_id, authorization):
        self.calls.append(('execute', plan_id, authorization))
        return {'operation_id': plan_id, 'plan_id': plan_id, 'status': 'SUCCEEDED',
                'service_id': 'sports', 'operation': 'create_reservation', 'preview': {},
                'expires_at': 0, 'revision': 1, 'verification_level': 'REMOTE_RECORD_VERIFIED',
                'automatic_resend': False}

    def result(self, operation_id):
        self.calls.append(('result', operation_id))
        return {'operation_id': operation_id, 'plan_id': operation_id, 'status': 'UNKNOWN',
                'service_id': 'sports', 'operation': 'create_reservation', 'preview': {},
                'expires_at': 0, 'revision': 1, 'verification_level': 'LOCAL_EXECUTION_STATE',
                'automatic_resend': False}


class FakeMedia:
    def __init__(self):
        self.cancelled = []

    def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return {'task_id': task_id, 'cancelled': True}


class FakeSubmissions:
    def __init__(self, records=None):
        self.store = FakeStore(records)
        self.engine = FakeEngine(records)

    def prepare(self, **kwargs):
        return {'operation_id': 'b' * 32, 'status': 'PREPARED', 'service_id': 'learn',
                'operation': 'submit_assignment', **kwargs}


# -- harness ---------------------------------------------------------------
def assistant(monkeypatch, tmp_path, *, resources=None, media=None, submissions=None):
    monkeypatch.setenv('THU_ASSISTANT_STATE_DIR', str(tmp_path / 'state'))
    for name in ('THU_LIBRARY_WRITE', 'THU_SPORTS_WRITE', 'THU_LIBRARY_RENEW', 'THU_LEARN_SUBMIT'):
        monkeypatch.delenv(name, raising=False)

    def bridge(command, **kwargs):
        if command == 'action_identity':
            return {'account_id': 'synthetic-facade-account'}
        raise CampusError('CAMPUS_GATEWAY_UNAVAILABLE')

    value = CampusAssistant(learning_factory=lambda: None, bridge=bridge,
                            resources=resources, media=media)
    value.todos = FakeTodos()
    if submissions is not None:
        value.facade = BusinessFacade(value, resources=resources, media=media,
                                      submissions=submissions)
    return value


def plan_record(service='library', operation='renew_loan', state='PREPARED'):
    return {'version': 1, 'operation_id': 'c' * 32, 'service_id': service,
            'operation': operation, 'account': 'synthetic-facade-account', 'business': {},
            'digest': 'd', 'context': None, 'adapter_version': 'library-space-action-v1',
            'expires_at': 0, 'state': state, 'missing_fields': [], 'reason': None,
            'revision': 0, 'events': [], 'challenge': None, 'effects': []}


# -- registration ----------------------------------------------------------
def test_the_frozen_six_methods_are_the_only_campus_methods(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    described = a.facade.describe()
    assert tuple(described['methods']) == ('query', 'health', 'prepare', 'execute', 'result', 'cancel')
    assert set(described['methods']) == set(METHODS)
    assert described['version'] == VERSION
    assert described['tool_count'] == 29 and described['legacy_tools_unchanged'] is True
    assert described['local_methods']['export_agenda']['side_effect'] == 'LOCAL_FILE_WRITE'


def test_registration_never_turns_a_module_file_into_an_available_capability(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    submission = a.facade.capability('learn', 'submit_assignment')
    assert submission is not None
    assert (submission.state, submission.executable, submission.prepare_allowed) == \
        ('BLOCKED', False, False)
    assert submission.reason == 'BLOCKED_SOURCE_EVIDENCE'
    public = submission.public()
    assert public['write_switch'] == 'THU_LEARN_SUBMIT'
    assert public['write_switch_enabled'] is False
    assert 'production final submit' in public['blocked_scope']

    resources = a.facade.capability('local', 'resources')
    assert (resources.state, resources.reason) == ('MISSING', 'RESOURCE_PROVIDER_UNAVAILABLE')
    assert resources.executable is False

    academics = a.facade.capability('academics', 'grades')
    assert academics.state == 'BLOCKED' and academics.extra and dict(academics.extra)['sensitive'] is True

    media = a.facade.capability('local', 'model_image_input')
    assert (media.state, media.reason) == ('BLOCKED', 'MODEL_IMAGE_INPUT_UNAVAILABLE')


def test_renewal_can_prepare_an_official_handoff_but_never_executes(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    renewal = a.facade.capability('library', 'renew_loan')
    assert (renewal.state, renewal.reason) == ('BLOCKED', 'SOURCE_CONTRACT_UNVERIFIED')
    assert renewal.prepare_allowed is True
    assert renewal.executable is False
    assert renewal.authorization == 'TRUSTED_HOST_AUTHORIZATION'


def test_describe_service_keeps_legacy_fields_and_adds_capability_state(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    described = a.facade.describe_service('library')
    assert described['service_id'] == 'library'
    assert described['registry_version'] == 'query-v1'
    for operation in described['operations']:
        assert {'operation', 'parameters_schema', 'category', 'pagination',
                'adapter_version', 'verification_level_ceiling'} <= set(operation)
        assert operation['facade']['state'] in ('AVAILABLE', 'DISABLED', 'BLOCKED', 'MISSING')
    actions = {row['operation']: row for row in described['actions']}
    assert actions['renew_loan']['facade']['state'] == 'BLOCKED'
    assert described['facade']['methods'] == list(METHODS)
    assert described['facade']['live_probe'] is False
    assert not any(operation.get('category') == 'write' for operation in described['operations'])


def test_local_service_registers_agenda_changes_and_resources_in_the_existing_entry(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    described = a.facade.describe_service('local')
    names = [operation['operation'] for operation in described['operations']]
    assert names == ['agenda', 'agenda_changes', 'resources']
    assert {operation['category'] for operation in described['operations']} == {'read'}
    agenda = next(operation for operation in described['operations'] if operation['operation'] == 'agenda')
    assert agenda['facade']['kind'] == 'READ' and agenda['facade']['executable'] is True
    assert agenda['local_read'] is True
    assert 'change_cursor' in next(operation for operation in described['operations']
                                   if operation['operation'] == 'agenda_changes')['parameters_schema']['properties']


# -- health ----------------------------------------------------------------
def test_health_is_static_and_covers_every_known_service(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    report = a.facade.health()
    assert report['status'] in ('OK', 'PARTIAL', 'UNKNOWN')
    assert report['live_probe'] is False and report['probe'] == 'STATIC_REGISTRATION_ONLY'
    assert {'status', 'checked_at', 'services', 'warnings'} <= set(report)
    rows = {row['service_id']: row for row in report['services']}
    assert {'learn', 'library', 'sports', 'local', 'academics'} <= set(rows)
    assert rows['library']['blockers']
    assert rows['academics']['state'] == 'BLOCKED'
    assert rows['local']['warnings'] == []


def test_health_survives_one_broken_service(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    real = a.facade._health_row

    def broken(service_id, entries, checked):
        if service_id == 'sports':
            raise RuntimeError('synthetic-private-host')
        return real(service_id, entries, checked)

    monkeypatch.setattr(a.facade, '_health_row', broken)
    report = a.facade.health()
    rows = {row['service_id']: row for row in report['services']}
    assert 'sports' in rows and rows['sports']['state'] == 'UNKNOWN'
    assert 'sports:HEALTH_PROBE_FAILED' in report['warnings']
    assert rows['learn']['read_operations'] > 0
    assert rows['library']['read_operations'] > 0
    assert 'synthetic-private-host' not in json.dumps(report)


def test_health_of_an_unknown_service_is_a_named_refusal(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.health('not-a-service')
    assert caught.value.code == 'FACADE_SERVICE_UNKNOWN'


# -- query -----------------------------------------------------------------
def test_agenda_query_pages_the_materialised_window(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    first = a.facade.query('local', 'agenda', {'start_date': '2026-11-17', 'end_date': '2026-11-23'},
                           page_size=2)
    assert [item['event_uid'] for item in first['calendar_events']] == ['ev-1', 'ev-2']
    assert first['items'] == first['calendar_events']
    assert first['views']['upcoming'] == ['ev-1', 'ev-2', 'ev-3']
    assert first['next_cursor'] and first['snapshot_complete'] is False
    assert first['pagination']['scope'] == 'FULL_WINDOW'
    assert first['facade']['service_id'] == 'local' and first['facade']['kind'] == 'READ'

    second = a.facade.query('local', 'agenda', {'start_date': '2026-11-17', 'end_date': '2026-11-23'},
                            cursor=first['next_cursor'], page_size=2)
    assert [item['event_uid'] for item in second['calendar_events']] == ['ev-3']
    assert second['next_cursor'] is None and second['snapshot_complete'] is True

    with pytest.raises(CampusError) as caught:
        a.facade.query('local', 'agenda', {'start_date': '2026-11-18', 'end_date': '2026-11-23'},
                       cursor=first['next_cursor'], page_size=2)
    assert caught.value.code == 'CURSOR_QUERY_MISMATCH'


def test_agenda_changes_keeps_the_change_cursor_separate_from_paging(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    value = a.facade.query('local', 'agenda_changes',
                           {'start_date': '2026-11-17', 'end_date': '2026-11-23',
                            'change_cursor': 'evc1.previous'}, page_size=1)
    assert value['change_cursor'] == 'evc1.synthetic-change-cursor'
    assert value['next_cursor'] and value['next_cursor'] != value['change_cursor']
    assert [row['change_id'] for row in value['changes']] == ['c1']
    assert value['pagination']['scope'] == 'FULL_CHANGE_SET'
    calls = a.todos.calls[-1]
    assert calls[0] == 'changes' and calls[1]['cursor'] == 'evc1.previous'


def test_resources_without_a_provider_is_a_named_gap(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.query('local', 'resources', {'semester': '2026-2027-1'})
    assert caught.value.code == 'FACADE_CAPABILITY_UNAVAILABLE'
    assert caught.value.capability['reason'] == 'RESOURCE_PROVIDER_UNAVAILABLE'


def test_resources_search_reports_a_capped_index_instead_of_complete(monkeypatch, tmp_path):
    resources = FakeResources(items=[{'stable_resource_uid': 'r1'}], total=250)
    a = assistant(monkeypatch, tmp_path, resources=resources)
    value = a.facade.query('local', 'resources', {'semester': '2026-2027-1', 'query': '合成'})
    # 250 matches but only 200 rows were read: neither flag may claim completeness.
    assert value['complete'] is False and value['source_complete'] is False
    assert 'RESOURCE_SEARCH_LIMIT_APPLIED' in value['warnings']
    assert value['total'] == 250 and value['next_cursor'] is None
    assert resources.calls[0]['semester'] == '2026-2027-1'
    assert value['facade']['capability_state'] == 'AVAILABLE'


def test_query_rejects_write_operations_and_proxy_fields(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    before = list(a.todos.calls)
    for operation in ('create_reservation', 'cancel_reservation', 'submit_assignment', 'renew_loan'):
        with pytest.raises(facade_errors.FacadeError) as caught:
            a.facade.query('local', operation, {})
        assert caught.value.code == 'FACADE_WRITE_NOT_REGISTERED'
    for parameters in ({'method': 'POST'}, {'headers': {'Authorization': 'x'}},
                       {'token': 'synthetic'}, {'nested': {'cookie': 'x'}},
                       {'list': [{'access_token': 'x'}]}):
        with pytest.raises(facade_errors.FacadeError) as caught:
            a.facade.query('local', 'agenda', parameters)
        assert caught.value.code == 'FACADE_QUERY_CANNOT_PROXY'
    assert a.todos.calls == before


def test_unregistered_operation_keeps_the_legacy_query_error(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    with pytest.raises(CampusError) as caught:
        a.facade.query('local', 'no_such_operation', {})
    assert caught.value.code == 'UNREGISTERED_QUERY_OPERATION'


# -- prepare / execute / result -------------------------------------------
def test_prepare_refuses_read_operations_and_unknown_operations(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.prepare('library', 'loans', {})
    assert caught.value.code == 'FACADE_WRITE_NOT_REGISTERED'
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.prepare('library', 'delete_everything', {})
    assert caught.value.code == 'FACADE_OPERATION_NOT_REGISTERED'


def test_blocked_submission_prepare_never_reaches_the_engine(monkeypatch, tmp_path):
    engine = FakeEngine()
    a = assistant(monkeypatch, tmp_path)
    a.actions = engine
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.prepare('learn', 'submit_assignment',
                         {'assignment_id': 'SYNTH-1', 'file_paths': []})
    assert caught.value.code == 'FACADE_CAPABILITY_UNAVAILABLE'
    assert caught.value.capability['blocked_scope']
    assert engine.calls == []


def test_renewal_prepare_is_allowed_and_execute_is_refused_before_preflight(monkeypatch, tmp_path):
    records = {'c' * 32: plan_record()}
    engine = FakeEngine(records)
    a = assistant(monkeypatch, tmp_path)
    a.actions = engine

    plan = a.facade.prepare('library', 'renew_loan', {'record_id': 'SYNTH-LOAN-1'})
    assert plan['status'] == 'PREPARED'
    assert plan['facade']['capability_state'] == 'BLOCKED'
    assert engine.calls[0][0] == 'prepare'

    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.execute('c' * 32, '对本次合成续借的明确人工授权')
    assert caught.value.code == 'FACADE_CAPABILITY_UNAVAILABLE'
    assert caught.value.capability['reason'] == 'SOURCE_CONTRACT_UNVERIFIED'
    assert [call[0] for call in engine.calls] == ['prepare']


def test_execute_refuses_an_unknown_plan_without_creating_one(monkeypatch, tmp_path):
    engine = FakeEngine()
    a = assistant(monkeypatch, tmp_path)
    a.actions = engine
    with pytest.raises(CampusError) as caught:
        a.facade.execute('f' * 32, 'synthetic authorization text')
    assert caught.value.code == 'ACTION_NOT_FOUND'
    assert engine.calls == []


def test_execute_routes_a_learn_plan_to_the_submission_engine(monkeypatch, tmp_path):
    from thu_assistant.facade.registry import Registration
    records = {'a' * 32: plan_record(service='learn', operation='submit_assignment')}
    provider = FakeSubmissions(records)
    provider.engine.calls = []
    a = assistant(monkeypatch, tmp_path, submissions=provider)
    a.actions = FakeEngine()
    # A future verified submission contract would flip the entry to AVAILABLE;
    # this case proves the routing, not that the blocked capability is enabled.
    enabled = Registration('learn', 'submit_assignment', 'CAMPUS_WRITE', '5.3',
                           'learn-submission-v1', 'AVAILABLE', executable=True,
                           prepare_allowed=True, authorization='TRUSTED_HOST_AUTHORIZATION')
    monkeypatch.setattr(a.facade, 'capability', lambda service, operation: enabled)
    value = a.facade.execute('a' * 32, '对本次合成作业提交的明确人工授权')
    assert value['status'] == 'SUCCEEDED'
    assert provider.engine.calls[0][0] == 'execute'
    assert a.actions.calls == []


def test_result_reads_the_owning_engine_and_never_dispatches(monkeypatch, tmp_path):
    records = {'a' * 32: plan_record(service='learn', operation='submit_assignment', state='UNKNOWN')}
    provider = FakeSubmissions(records)
    provider.engine.calls = []
    a = assistant(monkeypatch, tmp_path, submissions=provider)
    a.actions = FakeEngine()
    value = a.facade.result('a' * 32)
    assert value['status'] == 'UNKNOWN'
    assert [call[0] for call in provider.engine.calls] == ['result']
    assert a.actions.calls == []


# -- cancel / request identity --------------------------------------------
def test_cancel_of_a_queued_request_runs_cleanup_once_and_never_sends(monkeypatch, tmp_path):
    registry = RequestRegistry()
    request = registry.begin('req-queued')
    cleaned = []
    request.add_cleanup(lambda: cleaned.append('once'))
    value = registry.cancel('req-queued', 'synthetic')
    assert value['cancelled'] is True and value['state'] == CANCELLED
    assert value['sent_before_cancel'] is False
    assert cleaned == ['once'] and request.event.is_set()
    again = registry.cancel('req-queued')
    assert again['state'] == CANCELLED and cleaned == ['once']


def test_cancel_of_a_running_request_is_recorded_as_requested_not_undone(monkeypatch, tmp_path):
    registry = RequestRegistry()
    request = registry.begin('req-running')
    registry.start(request)
    cleaned = []
    request.add_cleanup(lambda: cleaned.append('later'))
    value = registry.cancel('req-running')
    assert value['state'] == CANCEL_REQUESTED and value['cancelled'] is True
    assert value['sent_before_cancel'] is True
    assert cleaned == []          # staging stays until the handler returns
    registry.finish(request)
    assert request.state == FINISHED and cleaned == ['later']


def test_cancel_of_an_unknown_or_finished_request_is_not_success(monkeypatch, tmp_path):
    registry = RequestRegistry()
    missing = registry.cancel('req-missing')
    assert missing['cancelled'] is False and missing['state'] == 'NOT_FOUND'
    request = registry.begin('req-done')
    registry.finish(request)
    done = registry.cancel('req-done')
    assert done['cancelled'] is False and done['state'] == FINISHED
    assert '不会重放' in done['note']


def test_request_registry_bounds_active_requests(monkeypatch, tmp_path):
    registry = RequestRegistry(max_active=1)
    registry.begin('req-1')
    with pytest.raises(facade_errors.FacadeError) as caught:
        registry.begin('req-2')
    assert caught.value.code == 'FACADE_REQUEST_LIMIT'


def test_facade_cancel_binds_media_cleanup(monkeypatch, tmp_path):
    media = FakeMedia()
    a = assistant(monkeypatch, tmp_path, media=media)
    request = a.facade.requests.begin('req-media')
    a.facade.requests.start(request)
    value = a.facade.cancel('req-media')
    assert value['media_cleanup'] == 'CANCELLED'
    assert media.cancelled == ['req-media']
    assert value['remote_effect'] == 'NONE' and value['facade']['kind'] == 'LOCAL_ONLY'
    a.facade.requests.finish(request)


def test_query_result_carries_the_bound_request_id(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    request = a.facade.requests.begin('req-bound')
    with scope(request):
        value = a.facade.query('local', 'agenda', {})
    assert value['facade']['request_id'] == 'req-bound'
    a.facade.requests.finish(request)


# -- local export ----------------------------------------------------------
def test_export_agenda_is_a_local_write_with_an_explicit_path(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    target = str(tmp_path / 'agenda.ics')
    value = a.facade.export_agenda(target, start_date='2026-11-17', end_date='2026-11-23')
    assert value['facade']['kind'] == 'LOCAL_WRITE'
    assert a.todos.exports[0][0] == target
    assert a.todos.exports[0][1]['format'] == 'ics'
    with pytest.raises(CampusError) as caught:
        a.facade.export_agenda(123)
    assert caught.value.code == 'EXPORT_DESTINATION_REQUIRED'


def test_export_without_an_agenda_provider_is_a_named_gap(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    a.todos = None
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.export_agenda(str(tmp_path / 'x.ics'))
    assert caught.value.code == 'FACADE_CAPABILITY_UNAVAILABLE'


# -- MCP boundary ----------------------------------------------------------
def test_mcp_tool_inventory_and_names_stay_at_the_legacy_baseline(monkeypatch, tmp_path):
    baseline = json.loads((ROOT / 'contracts' / 'v0.2' / 'compatibility-baseline.json').read_text())
    assert len(TOOLS) == baseline['mcp_tool_count'] == 29
    assert [tool['name'] for tool in TOOLS] == baseline['mcp_tools']


def test_mcp_describe_service_and_query_results_keep_legacy_shape(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    server = McpServer(a)
    described = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                               'params': {'name': 'describe_service', 'arguments': {'service_id': 'library'}}})
    value = described['result']['structuredContent']
    assert value['service_id'] == 'library' and value['operations'] and value['actions']
    assert value['facade']['version'] == VERSION

    queried = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                             'params': {'name': 'query_service',
                                        'arguments': {'service_id': 'local', 'operation': 'agenda',
                                                      'parameters': {}}}})
    result = queried['result']['structuredContent']
    assert result['status'] == 'OK' and result['complete'] is True
    assert {'status', 'source', 'checked_at', 'complete', 'source_complete', 'next_cursor',
            'warnings', 'verification_level'} <= set(result)
    assert queried['result']['isError'] is False


def test_mcp_error_carries_classification_and_the_capability_gap(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    server = McpServer(a)
    response = server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                              'params': {'name': 'query_service',
                                         'arguments': {'service_id': 'local', 'operation': 'resources',
                                                       'parameters': {'semester': '2026-2027-1'}}}})
    assert response['result']['isError'] is True
    value = response['result']['structuredContent']
    assert value['error']['code'] == 'FACADE_CAPABILITY_UNAVAILABLE'
    assert value['error']['category'] == 'NOT_IMPLEMENTED'
    assert value['error']['retryable'] is False
    assert value['error']['automatic_replay_allowed'] is False
    assert value['capability']['reason'] == 'RESOURCE_PROVIDER_UNAVAILABLE'
    text = response['result']['content'][0]['text']
    assert 'synthetic-facade-account' not in text
    assert str(tmp_path) not in text


def test_mcp_unknown_tool_and_bad_arguments_keep_their_codes(monkeypatch, tmp_path):
    server = McpServer(assistant(monkeypatch, tmp_path))
    unknown = server.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                             'params': {'name': 'no_such_tool', 'arguments': {}}})
    assert unknown['result']['structuredContent']['error']['code'] == 'UNKNOWN_TOOL'
    extra = server.handle({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                           'params': {'name': 'query_service',
                                      'arguments': {'service_id': 'local', 'operation': 'agenda',
                                                    'parameters': {}, 'method': 'POST'}}})
    assert extra['result']['structuredContent']['error']['code'] == 'INVALID_ARGUMENTS'


# -- error registry --------------------------------------------------------
def test_every_facade_error_code_is_registered_and_mirrored(monkeypatch, tmp_path):
    codes = json.loads(CONTRACT.read_text())['codes']
    for code in facade_errors.CODES:
        assert codes.get(code), code
        assert source_state.ERROR_CODES.get(code) == codes[code], code
        public = source_state.public_error(code)
        assert public['code'] == code and public['category'] == codes[code]
    assert set(codes) == set(source_state.ERROR_CODES)


# -- full description, representative read, authorization sequence ---------
def test_every_known_service_can_be_described_without_a_live_probe(monkeypatch, tmp_path):
    a = assistant(monkeypatch, tmp_path)
    described = []
    for service_id in a.facade.service_ids():
        value = a.facade.describe_service(service_id)
        assert value['service_id'] == service_id
        assert value['facade']['live_probe'] is False
        assert value['registry_version'] == 'query-v1'
        for operation in value['operations']:
            assert operation['facade']['state'] in ('AVAILABLE', 'DISABLED', 'BLOCKED', 'MISSING')
        described.append(service_id)
    assert {'learn', 'library', 'sports', 'info', 'local'} <= set(described)


def test_a_registered_learn_read_keeps_its_legacy_envelope_through_the_facade(monkeypatch, tmp_path):
    monkeypatch.setenv('THU_ASSISTANT_STATE_DIR', str(tmp_path / 'state'))

    def bridge(command, **kwargs):
        if command == 'action_identity':
            return {'account_id': 'synthetic-facade-account'}
        if command == 'list_courses':
            return {'courses': [{'course_id': 'course-1', 'course_name': '合成课程'}],
                    'count': 1, 'complete': True, 'pages': []}
        raise CampusError('CAMPUS_GATEWAY_UNAVAILABLE')

    class FakeLearning:
        def call_tool(self, name, arguments):
            assert name == 'list_courses'
            return {'courses': [{'course_id': 'course-1', 'course_name': '合成课程'}],
                    'count': 1, 'complete': True, 'pages': []}

        def get_login_status(self):
            return {'status': {'authenticated': True}}

    a = CampusAssistant(learning_factory=lambda: FakeLearning(), bridge=bridge)
    value = a.facade.query('learn', 'courses', {})
    assert value['status'] == 'OK' and value['complete'] is True
    assert value['items'] == [{'course_id': 'course-1', 'course_name': '合成课程'}]
    assert {'status', 'source', 'checked_at', 'complete', 'source_complete', 'next_cursor',
            'warnings', 'verification_level', 'missing_pages'} <= set(value)
    assert value['facade']['service_domain'] == 'LEARN'


class _SyntheticSportsAdapter:
    """One exact synthetic request; no campus host is contacted."""

    version = 'sports-action-v1'
    URL = 'https://sports.tsinghua.edu.cn/api/reserve'

    def __init__(self):
        self.sent = 0

    def preview(self, business):
        return business

    def preflight(self, record):
        from thu_assistant.actions import RequestSpec
        business = copy.deepcopy(record['business'])
        return {'business': business, 'context': record['context'],
                'checks': {name: True for name in ('availability', 'eligibility', 'fees', 'rules')},
                'requests': [RequestSpec(self.URL, 'POST', json.dumps(business),
                                         frame='0:synthetic')]}

    def dispatch(self, record, gate):
        assert self.store_state(record) == 'DISPATCHING'
        assert gate.consume(self.URL, 'POST', json.dumps(record['business']), frame='0:synthetic')
        self.sent += 1

    def store_state(self, record):
        return self.store.read(record['operation_id'])['state']

    def reconcile(self, record):
        row = {'business': record['business'], 'account': record['account'],
               'service_id': record['service_id'], 'operation': record['operation'],
               'record_id': 'synthetic-record', 'state': 'SUCCEEDED'}
        return {'records': [row], 'complete': True}


def test_prepare_execute_authorization_sequence_dispatches_exactly_once(monkeypatch, tmp_path):
    sys_path_entry = str(Path(__file__).resolve().parent)
    import sys
    if sys_path_entry not in sys.path:
        sys.path.insert(0, sys_path_entry)
    from synthetic_store import make_action_store
    from thu_assistant.actions import ActionEngine

    a = assistant(monkeypatch, tmp_path)
    store = make_action_store(tmp_path / 'actions')
    adapter = _SyntheticSportsAdapter()
    adapter.store = store

    confirmation = {'given': False}

    def consent(record):
        import time as _time
        if not confirmation['given']:
            return None          # the host has not confirmed this plan yet
        return {'plan_id': record['operation_id'], 'account': record['account'],
                'digest': record['digest'], 'adapter_version': record['adapter_version'],
                'source': 'human', 'reference': 'synthetic-host-event',
                'expires_at': _time.time() + 300}

    a.actions = ActionEngine(store, account_provider=lambda: 'synthetic-facade-account',
                             adapters={('sports', 'create_reservation'): adapter},
                             consent_verifier=consent)
    business = {'scene_id': 'scene-1', 'room_id': 'room-1', 'site_id': 'site-1',
                'session_id': 'session-1', 'date': '2099-01-01', 'mode': 'SPORT_GROUP',
                'members': 'self', 'payment_method': 'FREE'}
    plan = a.facade.prepare('sports', 'create_reservation', business)
    assert plan['status'] == 'PREPARED' and plan['facade']['capability_state'] == 'AVAILABLE'

    with pytest.raises(CampusError) as caught:
        a.facade.execute(plan['plan_id'], '对本次合成场地的明确人工授权')
    assert caught.value.code == 'TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED'
    assert adapter.sent == 0 and confirmation['given'] is False

    confirmation['given'] = True
    executed = a.facade.execute(plan['plan_id'], '对本次合成场地的明确人工授权')
    assert executed['status'] == 'SUCCEEDED' and adapter.sent == 1
    assert executed['automatic_resend'] is False
    again = a.facade.execute(plan['plan_id'], '对本次合成场地的明确人工授权')
    assert again['status'] == 'SUCCEEDED' and adapter.sent == 1


def test_resources_export_is_local_and_requires_the_provider(monkeypatch, tmp_path):
    class ExportResources(FakeResources):
        def __init__(self):
            super().__init__()
            self.exports = []

        def export_summary(self, semester, *, destination, **options):
            self.exports.append((semester, destination, options))
            return {'status': 'EXPORTED', 'destination': str(destination)}

    a = assistant(monkeypatch, tmp_path)
    with pytest.raises(facade_errors.FacadeError) as caught:
        a.facade.export_resources('2026-2027-1', str(tmp_path / 'summary.md'))
    assert caught.value.code == 'FACADE_CAPABILITY_UNAVAILABLE'
    assert caught.value.capability['reason'] == 'RESOURCE_PROVIDER_UNAVAILABLE'

    provider = ExportResources()
    a = assistant(monkeypatch, tmp_path, resources=provider)
    value = a.facade.export_resources('2026-2027-1', str(tmp_path / 'summary.md'))
    assert value['facade']['kind'] == 'LOCAL_WRITE'
    assert provider.exports[0][0] == '2026-2027-1'


def test_legacy_unbacked_write_keeps_its_prepare_shape(monkeypatch, tmp_path):
    """A framework operation without an adapter still yields ADAPTER_REQUIRED.

    Old clients prepared these plans to see the missing adapter; refusing at
    the facade would change their plan shape, so only a blocked source contract
    is refused there.
    """
    import sys
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from synthetic_store import make_action_store
    from thu_assistant.actions import ActionEngine

    a = assistant(monkeypatch, tmp_path)
    a.actions = ActionEngine(make_action_store(tmp_path / 'actions'),
                             account_provider=lambda: 'synthetic-facade-account', adapters={})
    plan = a.facade.prepare('sports', 'create', {
        'target': 'synthetic-court', 'date': '2099-01-01', 'start_time': '12:00',
        'end_time': '13:00', 'members': 'self', 'headcount': 1, 'fee': '0',
        'payment_method': 'none'})
    assert plan['status'] in ('NEEDS_USER_ACTION', 'PREPARED')
    assert plan['reason'] == 'ADAPTER_REQUIRED'
    with pytest.raises(CampusError) as caught:
        a.facade.execute(plan['plan_id'], '对本次合成事项的明确人工授权')
    assert caught.value.code == 'ADAPTER_REQUIRED'
