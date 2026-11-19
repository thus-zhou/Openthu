"""Regression coverage for the total-audit P1-1, P2-1 and P2-11 fixes."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from learn_assistant import auth as learn_auth
from learn_assistant.core import LearnAssistant
from learn_assistant.providers.mock import MockProvider
from thu_assistant import catalog, paths
from thu_assistant.action_store import ActionStore
from thu_assistant.bridge import CampusError
from thu_assistant.facade import BusinessFacade
from thu_assistant.queries import QueryEngine, legacy_metadata
from thu_assistant.snapshot_diff import SnapshotStore
from thu_assistant.submissions.store import FileSetStore, SubmissionJournal
from thu_assistant.todos import TodoCache


def test_default_state_root_is_the_single_paths_default(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    for name in ('OPENTHU_STATE_DIR', 'THU_ASSISTANT_STATE_DIR',
                 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'OPENTHU_CONFIG'):
        monkeypatch.delenv(name, raising=False)
    expected = paths.default_paths().state_dir
    assert catalog.state_root() == expected
    assert ActionStore().root == expected / 'actions-v1'
    assert TodoCache().root == expected / 'todos-v1'
    assert FileSetStore().root == expected / 'submissions-v1' / 'files'
    assert SubmissionJournal().root == expected / 'submissions-v1' / 'plans'
    snapshot = SnapshotStore()
    assert snapshot._path('acct_demo', 'scope-demo').parent == expected / 'events-v1'


def test_state_root_keeps_both_environment_aliases(monkeypatch, tmp_path):
    legacy = tmp_path / 'legacy-state'
    canonical = tmp_path / 'canonical-state'
    monkeypatch.setenv('THU_ASSISTANT_STATE_DIR', str(legacy))
    monkeypatch.delenv('OPENTHU_STATE_DIR', raising=False)
    assert catalog.state_root() == legacy
    monkeypatch.setenv('OPENTHU_STATE_DIR', str(canonical))
    assert catalog.state_root() == canonical


def test_state_root_honours_the_configuration_file(monkeypatch, tmp_path):
    custom = tmp_path / 'configured-state'
    config_file = tmp_path / 'config.json'
    config_file.write_text(
        json.dumps({'storage': {'state_dir': str(custom)}}), encoding='utf-8')
    monkeypatch.setenv('OPENTHU_CONFIG_FILE', str(config_file))
    for name in ('OPENTHU_STATE_DIR', 'THU_ASSISTANT_STATE_DIR',
                 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'OPENTHU_CONFIG'):
        monkeypatch.delenv(name, raising=False)
    assert catalog.state_root() == custom.resolve()


class _CourseOwner:
    def __init__(self, payload):
        self.payload = payload

    def bridge(self, name, **kwargs):
        return {'account_id': 'acct_demo'}

    def learning_call(self, name, params):
        return dict(self.payload)

    browsers = None


def test_learn_courses_does_not_swallow_an_incomplete_adapter():
    payload = {'courses': [], 'count': 0, 'complete': False,
               'warnings': ['SYNTHETIC_INCOMPLETE']}
    result = QueryEngine(_CourseOwner(payload)).query('learn', 'courses', {})
    assert result['status'] == 'PARTIAL'
    assert result['complete'] is False
    assert result['source_complete'] is False
    assert 'SYNTHETIC_INCOMPLETE' in result['warnings']
    legacy = legacy_metadata('list_courses', payload)
    assert legacy['status'] == 'PARTIAL'
    assert legacy['complete'] is False and legacy['source_complete'] is False


def test_learn_courses_complete_empty_is_still_empty():
    payload = {'courses': [], 'count': 0, 'complete': True, 'warnings': []}
    result = QueryEngine(_CourseOwner(payload)).query('learn', 'courses', {})
    assert result['status'] == 'EMPTY'
    assert result['complete'] is True and result['source_complete'] is True
    legacy = legacy_metadata('list_courses', payload)
    assert legacy['status'] == 'EMPTY'


class _BrokenIdentityOwner:
    def bridge(self, name, **kwargs):
        raise RuntimeError('gateway down')

    def learning_call(self, name, params):
        return {'courses': [{'id': 'c1', 'name': 'x'}], 'count': 1}

    browsers = None


def test_account_bound_queries_fail_closed_without_an_identity():
    engine = QueryEngine(_BrokenIdentityOwner())
    with pytest.raises(CampusError) as error:
        engine.query('learn', 'courses', {})
    assert error.value.code == 'AUTH_REQUIRED'
    with pytest.raises(CampusError) as local:
        engine.query('local', 'agenda', {})
    assert local.value.code == 'AUTH_REQUIRED'


def test_learn_assistant_follows_unified_state_and_download_roots(monkeypatch, tmp_path):
    configured_state = tmp_path / 'cfg-state'
    configured_downloads = tmp_path / 'cfg-downloads'
    config_file = tmp_path / 'config.json'
    config_file.write_text(
        json.dumps({
            'storage': {
                'state_dir': str(configured_state),
                'download_dir': str(configured_downloads),
            }
        }),
        encoding='utf-8',
    )
    for name in ('OPENTHU_DATA_DIR', 'OPENTHU_STATE_DIR', 'THU_ASSISTANT_STATE_DIR',
                 'OPENTHU_DOWNLOAD_DIR', 'OPENTHU_LEARN_SESSION_DIR', 'THU_LEARN_SESSION_DIR'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OPENTHU_CONFIG_FILE', str(config_file))

    assistant = LearnAssistant(MockProvider())
    assert assistant.system_data_root == configured_state.resolve()

    sample = tmp_path / 'report.pdf'
    sample.write_bytes(b'%PDF-1.4\n%synthetic\n')
    summary = assistant.prepare_submission(
        assignment_id='assignment-geology-1',
        file_paths=[sample],
    )
    preview_file = configured_state / 'submission-previews' / f"{summary['operation_id']}.json"
    assert preview_file.is_file()
    assert summary['preview'] == f"submission-previews/{summary['operation_id']}.json"
    assert str(tmp_path) not in json.dumps(summary, ensure_ascii=False)
    if os.name != 'nt':
        assert stat.S_IMODE(preview_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(preview_file.parent.stat().st_mode) == 0o700

    download_result = assistant.download_files(file_ids=['file-geology-intro'])
    assert Path(download_result['destination']) == configured_downloads.resolve()
    assert download_result['ok'] is True

    explicit_data = tmp_path / 'explicit-data'
    monkeypatch.setenv('OPENTHU_DATA_DIR', str(explicit_data))
    assert LearnAssistant(MockProvider()).system_data_root == explicit_data.resolve()

    if os.name != 'nt':
        assert learn_auth.default_session_dir() == configured_state.resolve() / 'LearnAssistant'


def test_facade_tracks_live_env_changes_and_injected_env_switches(monkeypatch):
    owner = SimpleNamespace(actions=object(), queries=object(), todos=object())
    for key in ('THU_LIBRARY_WRITE', 'THU_LIBRARY_RENEW', 'THU_SPORTS_WRITE',
                'THU_LIBRARY_READ', 'THU_SPACE_READ', 'THU_SPORTS_READ', 'THU_TODOS'):
        monkeypatch.delenv(key, raising=False)

    live_facade = BusinessFacade(owner)
    assert live_facade.capability('library', 'create_seat_reservation').state == 'AVAILABLE'
    assert live_facade.capability('library', 'renew_loan').state == 'BLOCKED'
    assert live_facade.capability('sports', 'create_reservation').state == 'AVAILABLE'
    assert live_facade.capability('local', 'agenda').state == 'AVAILABLE'

    # Mutate os.environ within the 5-second cache TTL and verify immediate invalidation.
    monkeypatch.setenv('THU_LIBRARY_WRITE', '0')
    monkeypatch.setenv('THU_SPORTS_WRITE', '0')
    monkeypatch.setenv('THU_TODOS', '0')
    assert live_facade.capability('library', 'create_seat_reservation').state == 'DISABLED'
    assert live_facade.capability('library', 'create_seat_reservation').reason == 'LIBRARY_WRITES_DISABLED'
    assert live_facade.capability('library', 'renew_loan').state == 'DISABLED'
    assert live_facade.capability('library', 'renew_loan').reason == 'LIBRARY_WRITES_DISABLED'
    assert live_facade.capability('sports', 'create_reservation').state == 'DISABLED'
    assert live_facade.capability('sports', 'create_reservation').reason == 'SPORTS_WRITES_DISABLED'
    assert live_facade.capability('local', 'agenda').state == 'DISABLED'

    for key in ('THU_LIBRARY_WRITE', 'THU_SPORTS_WRITE', 'THU_TODOS'):
        monkeypatch.delenv(key, raising=False)

    # Injected env must override switch checks without mutating os.environ.
    injected = BusinessFacade(owner, env={
        'THU_LIBRARY_WRITE': '0',
        'THU_LIBRARY_RENEW': '0',
        'THU_SPORTS_WRITE': '0',
    })
    assert injected.capability('library', 'create_seat_reservation').state == 'DISABLED'
    assert injected.capability('library', 'renew_loan').state == 'DISABLED'
    assert injected.capability('sports', 'create_reservation').state == 'DISABLED'
    described = injected.describe_service('library')
    by_op = {row['operation']: row for row in described['actions']}
    assert by_op['create_seat_reservation']['enabled'] is False
    assert by_op['create_seat_reservation']['reason'] == 'LIBRARY_WRITES_DISABLED'
