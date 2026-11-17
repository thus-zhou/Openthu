"""Fault-injection matrix with side-effect counters (module 7.2).

Each row exercises one failure mode against the component that owns it:

===============  ==========================================================
timeout          local read API deadline -> 504, one facade call, no retry
stream cut       lost upload response -> UNKNOWN, subsequent reads only
cancellation     media registration -> MediaCancelled, zero writes
lock conflict    protected store -> SecureStoreBusy, previous record intact
process exit     leftover temporary file -> ignored, active record readable
disk full        ENOSPC -> SecureWriteError, previous record intact
partial write    short write -> SecureWriteError, no temporary leftovers
===============  ==========================================================

Every write path counts its requests.  The submission transport counts uploads,
drafts, submits and reads separately so a recovery path cannot silently replay
an uncertain write.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from synthetic_store import make_action_store, unwrap, wrap

from thu_assistant import consent
from thu_assistant.local_api import LocalApiServer, contract as local_contract
from thu_assistant.local_api.client import LocalApiClient
from thu_assistant.media_assets import MediaAssets
from thu_assistant.media_assets import synthetic as media_synthetic
from thu_assistant.media_assets.errors import MediaCancelled
from thu_assistant.secure_store import (
    SecureRotationIncomplete,
    SecureStore,
    SecureStoreBusy,
    SecureStoreError,
    SecureWriteError,
    create_key_file,
    load_provider,
)
from thu_assistant.secure_store import store as store_module
from thu_assistant.submissions import (
    FileSetStore,
    SubmissionJournal,
    Submissions,
    SyntheticSubmissionTransport,
)

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = "windows-dpapi" if os.name == "nt" else "file-key"
PURPOSE = "SESSION"
ACCOUNT_DOMAIN = "CAMPUS"
SERVICE_DOMAIN = "LEARN"
ACCOUNT = "SYNTHETIC-ACCOUNT-7.2"
RECORD = "synthetic-fault-record"
AUTH = "Synthetic human instruction for this exact test transaction"
ASSIGNMENT = "SYNTHETIC-ASSIGNMENT-1"

FAULT_CASES = {
    "timeout": "test_local_api_timeout_returns_504_after_one_facade_call",
    "stream_cut": "test_lost_upload_response_never_increases_upload_count",
    "cancellation": "test_cancelled_media_registration_writes_nothing",
    "lock_conflict": "test_lock_conflict_reports_busy_and_keeps_the_record",
    "process_exit": "test_process_exit_leftover_temp_does_not_break_the_record",
    "disk_full": "test_disk_full_keeps_the_previous_record",
    "partial_write": "test_partial_write_keeps_the_previous_record_and_no_temp_file",
}


# ---------------------------------------------------------------------------
# protected store
# ---------------------------------------------------------------------------

def make_store(tmp_path: Path):
    key_path = tmp_path / "keys" / "master.key"
    create_key_file(key_path, PLATFORM)
    provider = load_provider(key_path)
    return SecureStore(provider, tmp_path / "data")


def write_record(store: SecureStore, value: bytes = b"v1", record: str = RECORD) -> int:
    return store.write(record, value, purpose=PURPOSE, account_domain=ACCOUNT_DOMAIN,
                       service_domain=SERVICE_DOMAIN, account=ACCOUNT)


def read_record(store: SecureStore, record: str = RECORD) -> bytes:
    return store.read(record, purpose=PURPOSE, account_domain=ACCOUNT_DOMAIN,
                      service_domain=SERVICE_DOMAIN, account=ACCOUNT)


def test_disk_full_keeps_the_previous_record(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    write_record(store, b"verified-v1")

    def no_space(fd, data):  # noqa: ARG001 - mirrors os.write
        raise OSError(errno.ENOSPC, "synthetic disk full")

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "write", no_space)
        with pytest.raises(SecureWriteError):
            write_record(store, b"must-not-land")
    assert read_record(store) == b"verified-v1"
    assert store.health()["ok"] is True


def test_partial_write_keeps_the_previous_record_and_no_temp_file(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    write_record(store, b"verified-v1")
    real_write = store_module._write_all

    def short_write(fd, data):
        real_write(fd, data[:max(1, len(data) // 2)])
        raise OSError(errno.EIO, "synthetic short write")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_write_all", short_write)
        with pytest.raises(SecureWriteError):
            write_record(store, b"partial")
    assert read_record(store) == b"verified-v1"
    assert not [path for path in store.records_dir.iterdir() if path.name.endswith(".tmp")]


def test_fsync_failure_keeps_the_previous_record(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    write_record(store, b"verified-v1")

    def fsync_failure(fd):  # noqa: ARG001
        raise OSError(errno.EIO, "synthetic fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_fsync_file", fsync_failure)
        with pytest.raises(SecureWriteError):
            write_record(store, b"must-not-land")
    assert read_record(store) == b"verified-v1"


def test_lock_conflict_reports_busy_and_keeps_the_record(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    write_record(store, b"verified-v1")
    monkeypatch.setattr(store_module, "_LOCK_TIMEOUT", 0.2)
    holder_script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from thu_assistant.secure_store import store as s\n"
        "lock = s._Lock(Path(sys.argv[1]))\n"
        "lock.__enter__()\n"
        "print('LOCKED', flush=True)\n"
        "time.sleep(5)\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(store._lock_path())],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
    )
    try:
        assert process.stdout.readline().strip() == "LOCKED"
        with pytest.raises(SecureStoreBusy):
            write_record(store, b"blocked")
    finally:
        process.kill()
        process.wait(timeout=10)
    assert read_record(store) == b"verified-v1"
    assert write_record(store, b"verified-v2") == 2
    assert read_record(store) == b"verified-v2"


def test_process_exit_leftover_temp_does_not_break_the_record(tmp_path):
    store = make_store(tmp_path)
    write_record(store, b"verified-v1")
    leftover = store.records_dir / f".{RECORD}.secure.deadbeef.tmp"
    leftover.write_bytes(b"partial process output")
    assert read_record(store) == b"verified-v1"
    store.write(RECORD, b"verified-v2", purpose=PURPOSE, account_domain=ACCOUNT_DOMAIN,
                service_domain=SERVICE_DOMAIN, account=ACCOUNT, expected_revision=1)
    assert read_record(store) == b"verified-v2"
    assert leftover.exists()  # an old temp file is inert; the store never adopts it


def test_rotation_interruption_keeps_the_last_valid_record(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    write_record(store, b"verified-v1")
    store.write("second-record", b"verified-second", purpose=PURPOSE,
                account_domain=ACCOUNT_DOMAIN, service_domain=SERVICE_DOMAIN,
                account=ACCOUNT)
    new_key = tmp_path / "keys2" / "master.key"
    create_key_file(new_key, PLATFORM)
    new_provider = load_provider(new_key)
    real_atomic = store_module._atomic_write
    calls = {"count": 0}

    def fail_second_record(path, data):
        if str(path).endswith(".secure"):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError(errno.EIO, "synthetic rotation interruption")
        return real_atomic(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_atomic_write", fail_second_record)
        with pytest.raises(SecureRotationIncomplete):
            store.rotate(new_provider)
    assert store.provider.key_id != new_provider.key_id
    assert read_record(store) == b"verified-v1"
    assert store.health()["ok"] is True
    result = store.rotate(new_provider)
    assert result["ok"] is True
    assert read_record(store) == b"verified-v1"


def test_rotation_pointer_failure_keeps_the_old_pointer(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    write_record(store, b"verified-v1")
    new_key = tmp_path / "keys2" / "master.key"
    create_key_file(new_key, PLATFORM)
    new_provider = load_provider(new_key)
    real_replace = store_module._replace

    def fail_pointer(source, destination):
        if Path(destination).name == "store.json":
            raise OSError(errno.EIO, "synthetic pointer failure")
        return real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_replace", fail_pointer)
        with pytest.raises(SecureRotationIncomplete):
            store.rotate(new_provider)
    assert store.provider.key_id != new_provider.key_id
    assert read_record(store) == b"verified-v1"
    assert store.health()["ok"] is True


# ---------------------------------------------------------------------------
# submission writes
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _enable_rehearsal(monkeypatch):
    monkeypatch.setenv("THU_LEARN_SUBMIT", "1")


def world(tmp_path, behavior="lost_upload", fail_at=0):
    store = make_action_store(tmp_path / "actions")
    files = FileSetStore(tmp_path / "files", protect=wrap, unprotect=unwrap)
    journal = SubmissionJournal(tmp_path / "plans", protect=wrap, unprotect=unwrap)
    transport = SyntheticSubmissionTransport(behavior=behavior, fail_at=fail_at)
    client = Submissions(transport=transport, store=store, file_store=files,
                         journal=journal, account_provider=lambda: "synthetic-account")
    return SimpleNamespace(client=client, transport=transport, root=tmp_path)


def make_files(directory):
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, payload in (("a.pdf", b"alpha"), ("b.pdf", b"beta")):
        path = directory / name
        path.write_bytes(payload)
        paths.append(path)
    return paths


def grant(context, plan_id):
    record = context.client.store.read(plan_id)
    host = consent.HostConsent(context.client.store)
    host.put(record, host.issue_for(record))


def prepare(context, paths):
    return context.client.prepare(assignment_id=ASSIGNMENT, file_paths=paths)


def test_prepare_records_zero_remote_writes(tmp_path):
    context = world(tmp_path, behavior="ok")
    plan = prepare(context, make_files(tmp_path / "source"))
    assert plan["plan_id"]
    assert context.transport.counts["upload"] == 0
    assert context.transport.counts["draft"] == 0
    assert context.transport.counts["submit"] == 0
    assert context.transport.counts["sent"] == 0


def test_lost_upload_response_never_increases_upload_count(tmp_path):
    context = world(tmp_path, behavior="lost_upload", fail_at=0)
    plan = prepare(context, make_files(tmp_path / "source"))
    grant(context, plan["plan_id"])
    result = context.client.execute(plan["plan_id"], AUTH)
    assert result["status"] == "UNKNOWN"
    assert context.transport.counts["upload"] == 1
    before = dict(context.transport.counts)
    for _ in range(3):
        context.client.result(plan["plan_id"])
    assert context.transport.counts["upload"] == before["upload"] == 1
    assert context.transport.counts["draft"] == 0
    assert context.transport.counts["submit"] == 0
    assert context.transport.counts["sent"] == 1


def test_a_lost_submit_response_is_read_back_not_replayed(tmp_path):
    context = world(tmp_path, behavior="submit_lost")
    plan = prepare(context, make_files(tmp_path / "source"))
    grant(context, plan["plan_id"])
    result = context.client.execute(plan["plan_id"], AUTH)
    assert result["status"] == "SUCCEEDED"
    assert context.transport.counts["submit"] == 1
    assert context.transport.counts["sent"] == 4
    before = dict(context.transport.counts)
    context.client.result(plan["plan_id"])
    assert context.transport.counts["submit"] == before["submit"]
    assert context.transport.counts["upload"] == before["upload"]


# ---------------------------------------------------------------------------
# cancellation, timeout and network guard
# ---------------------------------------------------------------------------

def test_cancelled_media_registration_writes_nothing(tmp_path):
    writes = []

    def hook(fd, data):  # noqa: ARG001
        writes.append(bytes(data))
        return len(data)

    assets = MediaAssets(tmp_path / "temp", write_hook=hook)
    source = media_synthetic.write_file(tmp_path / "src", "synthetic.png",
                                        media_synthetic.png())
    with pytest.raises(MediaCancelled):
        assets.register_synthetic_file(
            source, account_scope="acct_" + "a" * 24, task_id="task-cancel-1",
            purpose="MODEL_INPUT", source_evidence_id="local.synthetic_fixture",
            external_transfer_allowed=True, cancel=lambda: True,
        )
    assert writes == []
    assert assets.audit_report()["references"] == 0


class _SlowFacade:
    def __init__(self):
        self.calls = 0

    def health(self, *, service_id=None, cancel=None):  # noqa: ARG002
        return {"status": "OK", "checked_at": "2026-11-17T09:00:00+08:00",
                "services": [], "warnings": []}

    def services(self, *, query=None, category=None, cancel=None):  # noqa: ARG002
        return {"status": "OK", "items": [], "warnings": []}

    def query(self, service_id, operation, parameters, *, cursor=None,
              page_size=50, cancel=None):  # noqa: ARG002
        self.calls += 1
        time.sleep(0.8)
        return {"status": "OK", "items": [], "warnings": []}


def test_local_api_timeout_returns_504_after_one_facade_call(tmp_path):
    facade = _SlowFacade()
    limits = local_contract.LocalApiLimits(ui_wait_seconds=0.15)
    server = LocalApiServer(facade, state_dir=tmp_path / "state", limits=limits)
    server.start()
    try:
        client = LocalApiClient.from_bootstrap(server.bootstrap_file)
        client.bootstrap()
        status, payload = client.get("/api/v1/query/learn/courses",
                                     {"parameters": "{}"})
        assert status == 504
        assert payload["status"] == "TIMEOUT"
        assert facade.calls == 1
    finally:
        server.stop()


class NetworkGuard:
    """Block non-loopback connect() calls and record every attempt."""

    def __init__(self):
        self.attempts: list[str] = []

    def __enter__(self):
        from unittest.mock import patch

        real_connect = socket.socket.connect

        def guarded(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                host = str(address[0])
                if host not in ("127.0.0.1", "::1", "localhost"):
                    self.attempts.append(host)
                    raise RuntimeError("OUTBOUND_NETWORK_BLOCKED")
            return real_connect(sock, address)

        self._patch = patch.object(socket.socket, "connect", guarded)
        self._patch.start()
        return self

    def __exit__(self, *exc_info):
        self._patch.stop()
        return False


def test_representative_reads_make_no_outbound_connection():
    from learn_assistant.core import LearnAssistant
    from learn_assistant.providers.mock import MockProvider

    with NetworkGuard() as guard:
        assistant = LearnAssistant(MockProvider())
        assert assistant.list_courses()["count"] == 3
        assert assistant.list_assignments(course_id="course-geology")["count"] == 1
    assert guard.attempts == []


def test_the_fault_matrix_names_every_required_fault():
    assert set(FAULT_CASES) == {"timeout", "stream_cut", "cancellation",
                                "lock_conflict", "process_exit", "disk_full",
                                "partial_write"}
    for fault, name in FAULT_CASES.items():
        assert callable(globals().get(name)), (fault, name)
