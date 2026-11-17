"""Batch-6 integration seams (6.1 facade, 6.2 local API, 6.3 distribution).

Each module proves itself on its own branch.  These cases cover what only the
batch integration owner can prove:

* the real 6.1 ``BusinessFacade`` implements the consumer protocol 6.2 declares
  (seven reads, result envelopes, one source per route) instead of leaving the
  dashboard on the fail-closed stub;
* ``/resources`` carries the 4.2 ``semester`` through the server to the facade
  and refuses a missing one instead of returning an empty success;
* ``/todos`` and ``/calendar`` read one agenda snapshot but report their own
  source, so a card cannot silently show another route's data;
* the 6.1 facade codes and the 6.2 ``LOCAL_API_*`` codes share one frozen
  registry and one runtime mirror, with no duplicate or shadowed entry;
* the batch-6 module records, the ownership table and the batch gate agree, and
  an accepted gate names a full integration commit while batch 7 is ready;
* ``openthu dashboard`` prints its one JSON object *before* it blocks, so the
  user gets a URL for the board it started.

No case contacts a campus service, a model endpoint or the internet.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from thu_assistant import local_api, source_state
from thu_assistant.bridge import CampusError
from thu_assistant.facade import BusinessFacade, errors as facade_errors
from thu_assistant.local_api import contract as local_contract
from thu_assistant.local_api import facade as consumer_protocol
from thu_assistant.local_api.client import LocalApiClient
from thu_assistant.mcp_stdio import CampusAssistant

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "plans" / "v0.2.0" / "交付"
BATCH6 = ("6.1", "6.2", "6.3")

AGENDA = {
    "status": "OK",
    "source": {"service_id": "agenda", "operation": "agenda",
               "adapter_version": "agenda-events-v1"},
    "adapter_version": "agenda-events-v1",
    "checked_at": "2026-11-17T09:00:00+08:00",
    "verification_level": "NONE",
    "complete": True,
    "source_complete": True,
    "next_cursor": None,
    "timezone": "Asia/Shanghai",
    "window": {"start_date": "2026-11-17", "end_date": "2026-11-23"},
    "as_of": "2026-11-17T09:00:00+08:00",
    "sources_status": [{"source": "learn.assignments", "complete": True, "status": "OK"}],
    "scope_notes": [],
    "selection": {"services": None},
    "items": [
        {"event_uid": "todo-1", "title": "合成待办一", "status": "OPEN",
         "calendar_valid": True},
        {"event_uid": "todo-2", "title": "合成待办二", "status": "OPEN",
         "calendar_valid": True},
    ],
    "calendar_events": [
        {"event_uid": "ev-1", "title": "合成作业一", "status": "OPEN",
         "calendar_valid": True},
        {"event_uid": "ev-2", "title": "合成借阅", "status": "OPEN",
         "calendar_valid": True},
        {"event_uid": "ev-3", "title": "合成考试", "status": "OPEN",
         "calendar_valid": True},
    ],
    "undated": [],
    "excluded": [],
    "warnings": [],
    "views": {"upcoming": ["ev-1", "ev-2", "ev-3"], "overdue": [], "undated": [],
              "completed": [], "by_source": {"learn.assignments": ["ev-1"]}},
    "conflicts": [],
    "pagination": {"total_items": 3, "returned_items": 3, "next_cursor": None},
    "background_polling": False,
    "message_delivery": False,
    "delivery_state": "NOT_DELIVERED",
}


class FakeTodos:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def agenda(self, **kwargs):
        self.calls.append(kwargs)
        return copy.deepcopy(AGENDA)


class FakeResources:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "format_version": "resource-search-v1",
            "semester": kwargs["semester"],
            "query": kwargs.get("query"),
            "status": "OK",
            "items": [{"stable_resource_uid": "r1", "title": "合成资料一"}],
            "total": 1,
            "total_known": True,
            "offset": 0,
            "limit": 50,
            "snapshot_complete": True,
            "source_complete": True,
            "complete": True,
            "checked_at": "2026-11-17T09:00:00+08:00",
            "warnings": [],
            "source_errors": [],
        }


def _assistant(monkeypatch, tmp_path, *, resources=None) -> CampusAssistant:
    monkeypatch.setenv("THU_ASSISTANT_STATE_DIR", str(tmp_path / "state"))
    for name in ("THU_TODOS", "THU_LIBRARY_WRITE", "THU_SPORTS_WRITE",
                 "THU_LIBRARY_RENEW", "THU_LEARN_SUBMIT"):
        monkeypatch.delenv(name, raising=False)

    def bridge(command, **kwargs):
        if command == "action_identity":
            return {"account_id": "synthetic-batch6-account"}
        raise CampusError("CAMPUS_GATEWAY_UNAVAILABLE")

    value = CampusAssistant(learning_factory=lambda: None, bridge=bridge,
                            resources=resources)
    value.todos = FakeTodos()
    return value


def _board(assistant: CampusAssistant, tmp_path) -> tuple[local_api.LocalApiServer,
                                                          LocalApiClient]:
    server = local_api.LocalApiServer(assistant.facade, state_dir=tmp_path / "board")
    server.start()
    client = LocalApiClient.from_bootstrap(server.bootstrap_file)
    client.bootstrap()
    return server, client


def _get(client: LocalApiClient, path: str, query: dict | None = None):
    status, payload = client.get(path, query or {})
    return status, payload


@pytest.fixture
def board(monkeypatch, tmp_path):
    assistant = _assistant(monkeypatch, tmp_path, resources=FakeResources())
    server, client = _board(assistant, tmp_path)
    try:
        yield assistant, server, client
    finally:
        server.stop()


def test_the_real_facade_satisfies_the_local_api_consumer_protocol(board):
    assistant, _server, _client = board
    assert isinstance(assistant.facade, consumer_protocol.BusinessFacade)
    for name in ("health", "services", "query", "describe", "todos", "calendar",
                 "resources"):
        assert callable(getattr(assistant.facade, name)), name

    # The 4.2 provider used to live on ``facade.resources``; the 6.2 protocol
    # owns that name now, so the provider must have moved, not disappeared.
    assert assistant.facade.resource_provider is not None
    assert assistant.facade.resource_provider is assistant.resources

    for envelope, service, operation in (
        (assistant.facade.services(), "local", "services"),
        (assistant.facade.todos(), "agenda", "todos"),
        (assistant.facade.calendar(), "agenda", "calendar"),
        (assistant.facade.resources(semester="2026-2027-1"),
         "resources", "resources"),
    ):
        assert local_contract.check_envelope(envelope) == [], (service, operation)
        assert envelope["source"]["service_id"] == service
        assert envelope["source"]["operation"] == operation
        assert envelope["verification_level"] in source_state.VERIFICATION_LEVELS


def test_every_board_route_gets_the_data_of_its_own_source(board):
    assistant, _server, client = board
    expected = {
        "/api/v1/services": ("local", "services"),
        "/api/v1/todos": ("agenda", "todos"),
        "/api/v1/calendar": ("agenda", "calendar"),
        "/api/v1/resources": ("resources", "resources"),
    }
    bodies = {}
    for path, (service, operation) in expected.items():
        query = {"semester": "2026-2027-1"} if service == "resources" else None
        status, body = _get(client, path, query)
        assert status == 200, (path, body)
        assert body["source"]["service_id"] == service, path
        assert body["source"]["operation"] == operation, path
        assert body["display"]["state"] in ("READY", "EMPTY", "PARTIAL"), path
        assert "error" not in body or body["status"] != "OK", path
        bodies[path] = body

    status, health = _get(client, "/api/v1/health")
    assert status == 200 and health["display"]["state"] in ("READY", "PARTIAL")
    assert health["data"]["probe"] == "STATIC_REGISTRATION_ONLY"

    # One agenda snapshot, two cards: the todo rows and the calendar events must
    # not be the same list under two names.
    todos = bodies["/api/v1/todos"]
    calendar = bodies["/api/v1/calendar"]
    assert [row["event_uid"] for row in todos["items"]] == ["todo-1", "todo-2"]
    assert [row["event_uid"] for row in calendar["items"]] == ["ev-1", "ev-2", "ev-3"]
    assert todos["display"]["provenance"]["operation"] == "todos"
    assert calendar["display"]["provenance"]["operation"] == "calendar"


def test_resources_route_forwards_the_semester_and_refuses_a_missing_one(board):
    assistant, _server, client = board
    status, body = _get(client, "/api/v1/resources",
                        {"semester": "2026-2027-1", "query": "合成"})
    assert status == 200
    assert body["status"] == "OK"
    assert assistant.facade.resource_provider.calls[-1]["semester"] == "2026-2027-1"
    assert assistant.facade.resource_provider.calls[-1]["query"] == "合成"

    status, body = _get(client, "/api/v1/resources")
    assert status == 200
    assert body["status"] == "ERROR"
    assert body["error"]["code"] == "INVALID_ARGUMENT_VALUE"
    assert "SEMESTER_REQUIRED" in body["warnings"]
    assert body["items"] == []

    status, _body = _get(client, "/api/v1/resources", {"semester": "2026/2027"})
    assert status == 400


def test_batch6_error_registries_hold_both_modules_without_shadowing():
    frozen = json.loads(
        (ROOT / "contracts" / "v0.2" / "error-classification.json").read_text(
            encoding="utf-8"))["codes"]
    facade_codes = set(facade_errors.CODES)
    local_codes = {code for code in frozen if code.startswith("LOCAL_API_")}

    assert len(facade_codes) >= 9
    assert len(local_codes) == 32
    assert not facade_codes & local_codes
    for code in facade_codes | local_codes:
        assert code in frozen, code
        assert source_state.ERROR_CODES[code] == frozen[code], code
    # The frozen registry and the runtime mirror must stay one set, not two.
    assert set(frozen) == set(source_state.ERROR_CODES)
    assert len(frozen) == 359


def test_batch6_records_ownership_and_gate_agree():
    ownership = json.loads((DELIVERY / "模块负责表.json").read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in ownership["modules"] if row["id"] in BATCH6}
    assert set(rows) == set(BATCH6)

    gate = json.loads((DELIVERY / "批次门禁.json").read_text(encoding="utf-8"))
    by_batch = {item["batch"]: item for item in gate["batches"]}
    batch = by_batch[6]

    for module in BATCH6:
        record = json.loads(
            (DELIVERY / "模块结果" / f"{module}.json").read_text(encoding="utf-8"))
        assert record["status"] == "accepted", module
        assert record["batch"] == 6, module
        assert rows[module]["status"] == "accepted", module
        assert record["delivery_commit"] == rows[module]["delivery_commit"], module
        assert rows[module]["accepted_at"], module

    assert batch["status"] in ("ready", "accepted")
    if batch["status"] == "accepted":
        assert all(state == "accepted" for state in batch["module_status"].values())
        assert re.fullmatch(r"[0-9a-f]{40}", batch["integration_commit"] or "")
        assert batch["accepted_at"]
        # Same monotonic rule: batch 7 is ready now and may be accepted later.
        assert by_batch[7]["status"] in ("ready", "accepted")
        if by_batch[7]["status"] == "ready":
            assert by_batch[8]["status"] == "locked"


def _board_environment(tmp_path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("THU_", "MADMODEL_", "OPENTHU_", "LEARN_ASSISTANT_")):
            env.pop(key)
    env.update({
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "THU_ASSISTANT_STATE_DIR": str(tmp_path / "state"),
        "OPENTHU_STATE_DIR": str(tmp_path / "state"),
    })
    return env


def test_dashboard_prints_the_board_before_it_blocks(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-m", "thu_assistant", "dashboard"],
        cwd=ROOT, env=_board_environment(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8")
    try:
        # The CLI prints one indented JSON object; read until it parses.
        chunks = []
        value = None
        while value is None:
            line = process.stdout.readline()
            assert line, process.stderr.read()
            chunks.append(line)
            try:
                value = json.loads(''.join(chunks))
            except json.JSONDecodeError:
                assert len(chunks) < 200, 'dashboard never printed its JSON object'
                continue
        assert value["ok"] is True and value["command"] == "dashboard"
        result = value["result"]
        assert result["status"] == "LISTENING" and result["mode"] == "SERVE"
        assert result["url"] == "http://127.0.0.1:%d" % result["port"]
        with socket.create_connection(("127.0.0.1", result["port"]), timeout=5):
            pass
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=15)
        process.stdout.close()
        process.stderr.close()
