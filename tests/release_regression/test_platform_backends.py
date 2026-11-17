"""Production storage backends and synthetic transports on both platforms.

Linux exercises the real file-key backend, owner/mode boundaries and sealed
records.  Windows exercises the real DPAPI backend.  The two platform-specific
groups are skipped with an explicit reason on the other host; the report gate
classifies those skips as platform-limited, never as a passing check.

Every fixture is synthetic.  No campus account, cookie or model credential is
used, and ``SyntheticSubmissionTransport`` is only a declared test transport.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

import pytest

from thu_assistant.secure_store import (
    SecureKeyLocationError,
    SecureKeyNotConfigured,
    SecureKeyPermissionError,
    SecureStore,
    SecureStoreError,
    create_key_file,
    load_provider,
    open_store_from_environment,
    validate_key_separation,
)
from thu_assistant.submissions import (
    BlockedSubmissionTransport,
    Submissions,
    SyntheticSubmissionTransport,
    capability_report,
)

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = "windows-dpapi" if os.name == "nt" else "file-key"
PURPOSE = "SESSION"
ACCOUNT_DOMAIN = "CAMPUS"
SERVICE_DOMAIN = "LEARN"
ACCOUNT = "SYNTHETIC-ACCOUNT-2.2"
PLC_RECORD = "synthetic-session-2-2"


def make_store(tmp_path: Path):
    key_path = tmp_path / "keys" / "master.key"
    create_key_file(key_path, PLATFORM)
    provider = load_provider(key_path)
    return key_path, provider, SecureStore(provider, tmp_path / "data")


def write_record(store: SecureStore, value: bytes = b"synthetic-session-value") -> int:
    return store.write(
        PLC_RECORD, value, purpose=PURPOSE, account_domain=ACCOUNT_DOMAIN,
        service_domain=SERVICE_DOMAIN, account=ACCOUNT,
    )


def read_record(store: SecureStore) -> bytes:
    return store.read(
        PLC_RECORD, purpose=PURPOSE, account_domain=ACCOUNT_DOMAIN,
        service_domain=SERVICE_DOMAIN, account=ACCOUNT,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-key backend; Windows DPAPI is covered by the Windows job")
def test_linux_real_file_key_seals_records_and_keeps_owner_only_mode(tmp_path):
    key_path, _provider, store = make_store(tmp_path)
    info = key_path.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert write_record(store) == 1
    record = store.records_dir / f"{PLC_RECORD}.secure"
    assert record.is_file()
    assert b"synthetic-session-value" not in record.read_bytes()
    assert read_record(store) == b"synthetic-session-value"
    assert store.health()["backend"] == "file-key"
    assert store.health()["ok"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-key backend; Windows DPAPI is covered by the Windows job")
def test_linux_broad_key_permissions_are_rejected(tmp_path):
    key_path, _provider, _store = make_store(tmp_path)
    key_path.chmod(0o644)
    with pytest.raises(SecureKeyPermissionError):
        load_provider(key_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-key backend; Windows DPAPI is covered by the Windows job")
def test_linux_linked_key_is_rejected(tmp_path):
    key_path, _provider, _store = make_store(tmp_path)
    link = tmp_path / "keys" / "linked.key"
    try:
        os.symlink(key_path, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not permitted in this environment")
    with pytest.raises(SecureKeyPermissionError):
        load_provider(link)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI only")
def test_windows_dpapi_seals_records_and_reports_the_backend(tmp_path):
    _key_path, _provider, store = make_store(tmp_path)
    assert write_record(store) == 1
    record = store.records_dir / f"{PLC_RECORD}.secure"
    assert b"synthetic-session-value" not in record.read_bytes()
    assert read_record(store) == b"synthetic-session-value"
    health = store.health()
    assert health["backend"] == "windows-dpapi"
    assert health["ok"] is True


def test_key_must_live_outside_the_data_directory(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.chmod(0o700)
    with pytest.raises(SecureKeyLocationError):
        validate_key_separation(data_dir / "master.key", data_dir)
    with pytest.raises(SecureKeyLocationError):
        validate_key_separation(data_dir / "keys" / "master.key", data_dir)


def test_tampered_ciphertext_fails_closed_without_echoing_plaintext(tmp_path):
    _key_path, _provider, store = make_store(tmp_path)
    write_record(store)
    record = store.records_dir / f"{PLC_RECORD}.secure"
    payload = bytearray(record.read_bytes())
    payload[-1] ^= 0x40
    record.write_bytes(bytes(payload))
    with pytest.raises(SecureStoreError) as error:
        read_record(store)
    assert "synthetic-session-value" not in str(error.value)
    assert store.health()["ok"] is False


def test_open_store_requires_explicit_key_and_data_directory():
    with pytest.raises(SecureKeyNotConfigured):
        open_store_from_environment({})


def test_synthetic_transport_is_declared_and_never_the_default():
    transport = SyntheticSubmissionTransport()
    assert transport.synthetic is True
    blocked = BlockedSubmissionTransport()
    assert blocked.synthetic is False
    with pytest.raises(Exception) as error:
        blocked.read_assignment("SYNTHETIC-ASSIGNMENT-1")
    assert getattr(error.value, "code", "") == "SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED"
    report = capability_report()
    assert report["production_transport"] == "NOT_IMPLEMENTED"
    assert report["rehearsal"] == "SYNTHETIC_TRANSPORT"
    assert report["write_switch_default"] == "OFF"
    assert report["implementation_state"] == "BLOCKED"


def test_validated_python_range_matches_the_running_interpreter():
    spec = json.loads((ROOT / "build_support" / "distribution.json").read_text(encoding="utf-8"))
    product = spec["product"]
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert current in product["validated_python"], current
    assert product["python_requires"] == ">=3.12"
    assert product["node_requires"] == ">=24"
