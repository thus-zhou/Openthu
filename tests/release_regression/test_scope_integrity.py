"""Account, semester and cursor scope isolation plus integrity rules.

These cases prove the cross-cutting promise that one account's cache, cursor,
media reference or dashboard session cannot be used by another, and that an
incomplete source is never mistaken for an empty or mass-deleted result.
Everything is synthetic and local.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path

import pytest

from thu_assistant import cache_policy, events, snapshot_diff, source_state
from thu_assistant.bridge import CampusError
from thu_assistant.local_api import LocalApiServer, security
from thu_assistant.local_api.client import LocalApiClient
from thu_assistant.media_assets import MediaAssets
from thu_assistant.media_assets import synthetic as media_synthetic
from thu_assistant.media_assets.errors import MediaReferenceAccountMismatch

NOW = datetime.fromisoformat("2026-11-17T09:00:00+08:00")
CHECK = NOW.isoformat()
TZ = timezone.utc
SCOPE = "acct_" + "a" * 24
OTHER_SCOPE = "acct_" + "b" * 24


def todo_id(value):
    return "todo_" + hashlib.sha256(str(value).encode()).hexdigest()[:32]


def todo_row(value="1", *, source="learn.assignments", title="合成作业",
             due_at="2026-11-20", status="TODO", **extra):
    row = {
        "event_id": todo_id(source + ":" + str(value)), "source": source,
        "source_id": json.dumps(["course", str(value)]), "title": title,
        "due_at": due_at, "start_at": None, "end_at": None,
        "date_precision": "date", "status": status, "source_status": {},
        "checked_at": CHECK, "freshness": "LIVE_AT_CHECKED_AT", "warnings": [],
        "source_url": "https://learn.tsinghua.edu.cn/",
    }
    row.update(extra)
    return row


def scope():
    return {"start_date": "2026-11-17", "end_date": "2026-11-23",
            "sources": ["learn.assignments"], "include_completed": False,
            "include_overdue": True, "semester": None, "allow_stale": False,
            "max_source_pages": 3}


def source_record(source="learn.assignments", complete=True, status="SUCCESS", pages=1):
    return {"status": status, "complete": complete, "pages_read": pages,
            "checked_at": CHECK, "error_codes": [], "warnings": []}


def snapshot(items, *, complete=True, status="SUCCESS", account="A", scope_value=None):
    current_scope = scope_value or scope()
    scope_hash = snapshot_diff.scope_digest({key: current_scope.get(key)
                                             for key in snapshot_diff.SCOPE_KEYS})
    return {"version": snapshot_diff.VERSION, "canonical_version": snapshot_diff.VERSION,
            "account": account, "scope_digest": scope_hash, "scope": current_scope,
            "checked_at": CHECK, "snapshot_id": "snap_synthetic",
            "events": deepcopy(items),
            "sources": {"learn.assignments": source_record(
                complete=complete, status=status, pages=1 if status != "FAILED" else 0)}}


def calendar_event(*, uid="CAMPUS:UNKNOWN:EXAM:exam-1:0", time=None):
    return {
        "event_uid": uid, "account_domain": "CAMPUS",
        "source": {"service_domain": "UNKNOWN", "kind": "EXAM", "record_id": "exam-1",
                   "stable_resource_uid": "exam-1"},
        "title": "合成考试", "time": time or {"kind": "DATE", "date": "2026-11-20",
                                             "timezone": "Asia/Shanghai"},
        "status": "CONFIRMED", "sequence": 0, "last_modified": CHECK,
        "source_evidence_id": "academics.exams", "location": None, "description": None,
        "ics_uid": None, "export": {"state": "NOT_EXPORTED", "exported_at": None},
        "warnings": [], "derivation": {"kind": "DERIVED", "rule_version": "exam-event-v1",
                                       "from": ["exam.state"], "not_a_source_fact": True,
                                       "occurrence": "0"},
    }


# ---------------------------------------------------------------------------
# cursors, cache, media and dashboard sessions
# ---------------------------------------------------------------------------

def test_cursor_from_another_account_or_scope_is_refused():
    store = snapshot_diff.IncrementalStore(
        store=snapshot_diff.SnapshotStore(backend=snapshot_diff.MemoryBackend()),
        now=lambda: NOW)
    first = store.query(snapshot([events.from_todo(todo_row("1"))]), account="A", scope=scope())
    cursor = first["next_cursor"]
    assert cursor
    with pytest.raises(CampusError, match="EVENT_CURSOR_SCOPE_MISMATCH"):
        store.query(snapshot([events.from_todo(todo_row("1"))]), account="B", scope=scope(),
                    cursor=cursor)
    other_scope = {**scope(), "sources": ["library.loans"]}
    with pytest.raises(CampusError, match="EVENT_CURSOR_SCOPE_MISMATCH"):
        store.query(snapshot([events.from_todo(todo_row("1"))]), account="A",
                    scope=other_scope, cursor=cursor)
    assert first["cursor_scope"] == "PROCESS_LOCAL"


def test_cache_record_from_another_account_or_semester_is_rejected():
    entry = cache_policy.record(
        account="A", parameters={"semester": "2026-2027-1"},
        source="library.loans", checked_at="2026-11-17T09:00:00+08:00",
        value={"items": [{"title": "合成借阅"}]})
    accepted = cache_policy.evaluate(
        entry, resource_kind="library.loans", account="A",
        parameters={"semester": "2026-2027-1"}, source="library.loans", now=NOW)
    assert accepted["allowed"] is True
    assert accepted["proves_authentication"] is False
    other_account = cache_policy.evaluate(
        entry, resource_kind="library.loans", account="B",
        parameters={"semester": "2026-2027-1"}, source="library.loans", now=NOW)
    assert other_account["allowed"] is False
    assert "CACHE_BINDING_MISMATCH" in other_account["warnings"]
    assert other_account["value"] is None
    other_semester = cache_policy.evaluate(
        entry, resource_kind="library.loans", account="A",
        parameters={"semester": "2025-2026-2"}, source="library.loans", now=NOW)
    assert other_semester["allowed"] is False
    assert "CACHE_BINDING_MISMATCH" in other_semester["warnings"]


def test_media_reference_from_another_account_cannot_resolve(tmp_path):
    assets = MediaAssets(tmp_path / "temp")
    source = media_synthetic.write_file(tmp_path / "src", "synthetic.png",
                                        media_synthetic.png())
    reference = assets.register_synthetic_file(
        source, account_scope=SCOPE, task_id="task-scope-1", purpose="MODEL_INPUT",
        source_evidence_id="local.synthetic_fixture", external_transfer_allowed=True)
    with pytest.raises(MediaReferenceAccountMismatch):
        assets.read_bytes(reference.media_id, account_scope=OTHER_SCOPE,
                          task_id="task-scope-1")
    assert assets.read_bytes(reference.media_id, account_scope=SCOPE,
                             task_id="task-scope-1")
    assets.close()


class _CountingFacade:
    def __init__(self):
        self.calls = 0

    def health(self, *, service_id=None, cancel=None):  # noqa: ARG002
        self.calls += 1
        return {"status": "OK", "checked_at": CHECK, "services": [], "warnings": []}

    def services(self, *, query=None, category=None, cancel=None):  # noqa: ARG002
        self.calls += 1
        return {"status": "OK", "items": [], "warnings": []}

    def query(self, service_id, operation, parameters, *, cursor=None,
              page_size=50, cancel=None):  # noqa: ARG002
        self.calls += 1
        return {"status": "OK", "items": [], "warnings": []}


def test_dashboard_session_from_another_instance_is_refused(tmp_path):
    first_facade, second_facade = _CountingFacade(), _CountingFacade()
    first = LocalApiServer(first_facade, state_dir=tmp_path / "first")
    second = LocalApiServer(second_facade, state_dir=tmp_path / "second")
    first.start()
    second.start()
    try:
        client = LocalApiClient.from_bootstrap(first.bootstrap_file)
        client.bootstrap()
        connection = http.client.HTTPConnection("127.0.0.1", second.port, timeout=5)
        try:
            connection.request("GET", "/api/v1/health",
                               headers={"Cookie": f"{security.COOKIE_NAME}={client.cookie}"})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        assert response.status == 401
        assert payload["error"]["code"] == "LOCAL_API_SESSION_INVALID"
        assert second_facade.calls == 0
    finally:
        first.stop()
        second.stop()


# ---------------------------------------------------------------------------
# time precision, stable identity and incomplete sources
# ---------------------------------------------------------------------------

def test_cross_midnight_interval_stays_in_its_window_and_timezone():
    event = events.from_calendar_event(calendar_event(time={
        "kind": "DATE_TIME", "start": "2026-11-23T23:30:00+08:00",
        "end": "2026-11-24T00:30:00+08:00", "timezone": "Asia/Shanghai"}),
        source_id="academics.exams")
    assert events.in_window(event, date(2026, 11, 17), date(2026, 11, 23)) is True
    assert event["time"]["start"] == "2026-11-23T23:30:00+08:00"
    assert event["time"]["end"] == "2026-11-24T00:30:00+08:00"
    assert event["time"]["timezone"] == "Asia/Shanghai"
    assert event["time_precision"] in {"minute", "second"}


def test_date_only_deadline_stays_date_level_and_is_not_midnight():
    item = events.from_todo(todo_row(due_at="2026-11-20"))
    assert item["time"]["kind"] == "DATE"
    assert item["time"]["date"] == "2026-11-20"
    assert "T00:00" not in json.dumps(item["time"])
    assert item["time_precision"] == "date"


def test_stable_identity_survives_title_change_and_separates_sources():
    first = events.from_todo(todo_row("1", title="旧标题"))
    second = events.from_todo(todo_row("1", title="新标题"))
    assert first["event_uid"] == second["event_uid"]
    other = events.from_todo(todo_row("1", source="library.loans", title="旧标题"))
    assert other["event_uid"] != first["event_uid"]
    duplicate = events.from_todo(todo_row("1", source="library.loans", title="旧标题"))
    assert duplicate["event_uid"] == other["event_uid"]


def test_incomplete_source_never_mass_deletes_or_reports_empty():
    baseline = snapshot([events.from_todo(todo_row("1")),
                         events.from_todo(todo_row("2"))])
    failed = {"events": [], "checked_at": CHECK,
              "sources": [source_record(complete=False, status="FAILED", pages=0)]}
    result = snapshot_diff.compare(baseline, failed, account="A", scope=scope(), now=NOW)
    types = [change["type"] for change in result["changes"]]
    assert "SOURCE_UNAVAILABLE" in types
    assert "DELETED" not in types
    assert result["counts"]["deleted"] == 0
    assert result["counts"]["carried_forward"] == 2
    assert result["status"] == "PARTIAL"
    assert result["complete"] is False
    completeness = source_state.completeness("PARTIAL", source_complete=False)
    assert completeness["complete"] is False
    assert "SOURCE_PARTIAL" in completeness["warnings"]
    assert completeness["partial_reasons"]


def test_missing_page_does_not_delete_unseen_events():
    baseline = snapshot([events.from_todo(todo_row("1")),
                         events.from_todo(todo_row("2"))])
    partial = snapshot([events.from_todo(todo_row("1"))], complete=False, status="PARTIAL")
    result = snapshot_diff.compare(baseline, partial, account="A", scope=scope(), now=NOW)
    assert "DELETED" not in [change["type"] for change in result["changes"]]
    assert "SOURCE_INCOMPLETE" in [change["type"] for change in result["changes"]]
    assert result["comparison"]["learn.assignments"]["deletions"] == "SUPPRESSED_NOT_COMPARABLE"
    assert len(result["snapshot"]["events"]) == 2


def test_snapshot_baseline_is_account_bound():
    baseline = snapshot([events.from_todo(todo_row("1"))])
    with pytest.raises(CampusError, match="SNAPSHOT_ACCOUNT_MISMATCH"):
        snapshot_diff.compare(baseline, snapshot([]), account="B", scope=scope(), now=NOW)
