"""CLI and stdio protocol checks for module 6.1.

Every case runs the real entry points in a subprocess with an isolated state
directory.  No campus request is made: the local commands never start the
gateway, and the commands that would read a source are asserted to fail with a
structured error instead of a traceback.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def environment(tmp_path):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(('THU_', 'MADMODEL_', 'OPENTHU_', 'LEARN_ASSISTANT_')):
            env.pop(key)
    env.update({
        'PYTHONPATH': str(ROOT / 'src'),
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONUTF8': '1',
        'THU_ASSISTANT_STATE_DIR': str(tmp_path / 'state'),
        'MADMODEL_STATE_DIR': str(tmp_path / 'madmodel'),
        'THU_ACCESS_STATE_DIR': str(tmp_path / 'access'),
    })
    return env


def cli(tmp_path, *arguments, timeout=60):
    return subprocess.run([sys.executable, '-m', 'thu_assistant', *arguments],
                          cwd=ROOT, env=environment(tmp_path), capture_output=True,
                          text=True, encoding='utf-8', timeout=timeout)


def payload(result):
    assert result.stdout.strip(), result.stderr
    return json.loads(result.stdout)


def test_help_keeps_the_legacy_commands_and_lists_the_new_ones(tmp_path):
    result = cli(tmp_path, '--help')
    assert result.returncode == 0
    for command in ('services', 'discover', 'login', 'read', 'courses', 'model',
                    'describe', 'health', 'query', 'agenda', 'dashboard'):
        assert command in result.stdout
    assert '{services,discover,login,read,courses,model,describe,health,query,agenda,dashboard}' \
        in result.stdout


def test_describe_local_lists_the_registered_local_reads(tmp_path):
    result = cli(tmp_path, 'describe', '--service', 'local')
    assert result.returncode == 0
    value = payload(result)
    assert value['ok'] is True and value['command'] == 'describe'
    operations = [row['operation'] for row in value['result']['operations']]
    assert operations == ['agenda', 'agenda_changes', 'resources']
    assert value['result']['facade']['version'] == 'business-facade-v1'
    assert value['result']['facade']['live_probe'] is False


def test_health_reports_a_static_capability_summary(tmp_path):
    result = cli(tmp_path, 'health', '--service', 'library')
    assert result.returncode == 0
    value = payload(result)['result']
    assert value['live_probe'] is False and value['probe'] == 'STATIC_REGISTRATION_ONLY'
    assert value['services'][0]['service_id'] == 'library'
    assert value['status'] in ('OK', 'PARTIAL', 'UNKNOWN')


def test_health_of_an_unknown_service_is_a_structured_error(tmp_path):
    result = cli(tmp_path, 'health', '--service', 'not-a-service')
    assert result.returncode == 1
    assert result.stderr == ''
    error = payload(result)['error']
    assert error['code'] == 'FACADE_SERVICE_UNKNOWN' and error['category'] == 'INPUT'


def test_query_rejects_malformed_and_non_object_parameters(tmp_path):
    broken = cli(tmp_path, 'query', '--service', 'local', '--operation', 'agenda',
                 '--parameters', '{not json')
    assert broken.returncode == 1
    assert payload(broken)['error']['code'] == 'INVALID_ARGUMENT_VALUE'

    array = cli(tmp_path, 'query', '--service', 'local', '--operation', 'agenda',
                '--parameters', '[]')
    assert array.returncode == 1
    assert payload(array)['error']['code'] == 'INVALID_ARGUMENT_TYPE'

    nothing = cli(tmp_path, 'read')
    assert nothing.returncode == 1
    assert payload(nothing)['error']['code'] == 'INVALID_ARGUMENT_VALUE'


def test_dashboard_refuses_a_public_bind_and_reports_the_missing_provider(tmp_path):
    public = cli(tmp_path, 'dashboard', '--host', '0.0.0.0')
    assert public.returncode == 1
    assert payload(public)['error']['code'] == 'INVALID_ARGUMENT_VALUE'

    missing = cli(tmp_path, 'dashboard')
    assert missing.returncode == 1
    error = payload(missing)['error']
    assert error['code'] == 'LOCAL_DASHBOARD_UNAVAILABLE'
    assert error['category'] == 'NOT_IMPLEMENTED'


def test_cli_output_never_carries_tracebacks_paths_or_secrets(tmp_path):
    result = cli(tmp_path, 'query', '--service', 'local', '--operation', 'resources',
                 '--parameters', '{"semester":"2026-2027-1"}')
    assert result.returncode == 1
    assert result.stderr == ''
    text = result.stdout
    assert 'Traceback' not in text
    assert str(tmp_path) not in text
    assert 'synthetic' not in text.casefold()
    error = payload(result)['error']
    assert error['code'] == 'FACADE_CAPABILITY_UNAVAILABLE'
    assert error['automatic_replay_allowed'] is False


def test_mcp_stdio_stays_a_clean_json_line_protocol(tmp_path):
    """Enumerate tools, describe a service, miss a capability, cancel nothing.

    The last request proves a ``notifications/cancelled`` line for an unknown
    request does not disturb the serialized response stream.
    """
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
         'params': {'name': 'describe_service', 'arguments': {'service_id': 'local'}}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
         'params': {'name': 'query_service',
                    'arguments': {'service_id': 'local', 'operation': 'resources',
                                  'parameters': {'semester': '2026-2027-1'}}}},
        {'jsonrpc': '2.0', 'method': 'notifications/cancelled',
         'params': {'requestId': 99, 'reason': 'synthetic'}},
        {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list', 'params': {}},
    ]
    result = subprocess.run([sys.executable, '-m', 'thu_assistant.mcp_stdio'],
                            cwd=ROOT, env=environment(tmp_path),
                            input=''.join(json.dumps(item) + '\n' for item in messages),
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 5
    rows = {}
    for line in lines:
        value = json.loads(line)          # every line is one JSON document
        if 'id' in value:
            rows[value['id']] = value
    assert set(rows) == {1, 2, 3, 4, 5}
    tools = rows[2]['result']['tools']
    assert len(tools) == 29
    assert [tool['name'] for tool in tools] == [tool['name'] for tool in rows[5]['result']['tools']]
    described = rows[3]['result']['structuredContent']
    assert described['service_id'] == 'local'
    assert [row['operation'] for row in described['operations']] == \
        ['agenda', 'agenda_changes', 'resources']
    assert rows[4]['result']['isError'] is True
    assert rows[4]['result']['structuredContent']['error']['code'] == 'FACADE_CAPABILITY_UNAVAILABLE'
    assert 'Traceback' not in result.stdout and 'Traceback' not in result.stderr
