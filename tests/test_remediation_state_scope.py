"""Regression coverage for the total-audit P1-1, P2-1 and P2-11 fixes."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from thu_assistant import catalog, paths
from thu_assistant.action_store import ActionStore
from thu_assistant.bridge import CampusError
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
    home = tmp_path / 'home'
    custom = tmp_path / 'configured-state'
    config_dir = home / '.config' / 'openthu'
    config_dir.mkdir(parents=True)
    (config_dir / 'config.json').write_text(
        json.dumps({'storage': {'state_dir': str(custom)}}), encoding='utf-8')
    monkeypatch.setenv('HOME', str(home))
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
