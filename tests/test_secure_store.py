"""Synthetic security tests for the v0.2 protected local store.

No school account, cookie, model credential or live network response is used.
The real AES-GCM backend runs with fresh random keys in temporary directories;
there is no test mode that disables encryption.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import thu_assistant.secure_store.bridge as bridge_module
import thu_assistant.secure_store.store as store_module
from thu_assistant.secure_store import (
    SecureAccountMismatch,
    SecureDecryptError,
    SecureEnvelopeCorrupt,
    SecureInvalidRecordId,
    SecureKeyLocationError,
    SecureKeyNotConfigured,
    SecureKeyPermissionError,
    SecureLegacyUnavailable,
    SecurePurposeMismatch,
    SecureRevisionConflict,
    SecureRotationIncomplete,
    SecureServiceMismatch,
    SecureStore,
    SecureWriteError,
    create_key_file,
    handle_request,
    load_provider,
    open_store_from_environment,
    pack_envelope,
    parse_envelope,
    provider_from_environment,
    subprocess_environment,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contracts" / "v0.2" / "storage-header.schema.json"
PURPOSE = "SESSION"
ACCOUNT_DOMAIN = "CAMPUS"
SERVICE_DOMAIN = "LEARN"
ACCOUNT = "synthetic-student"


def key_backend() -> str:
    return "windows-dpapi" if os.name == "nt" else "file-key"


@pytest.fixture()
def secure(tmp_path: Path):
    key_path = tmp_path / "keys" / "master.key"
    create_key_file(key_path, key_backend())
    provider = load_provider(key_path)
    data_dir = tmp_path / "data"
    store = SecureStore(provider, data_dir)
    return key_path, provider, store, data_dir


def write_session(
    store: SecureStore,
    record_id: str = "session-1",
    value: bytes = b"synthetic-session",
    account: str = ACCOUNT,
) -> int:
    return store.write(
        record_id,
        value,
        purpose=PURPOSE,
        account_domain=ACCOUNT_DOMAIN,
        service_domain=SERVICE_DOMAIN,
        account=account,
    )


def read_session(
    store: SecureStore,
    record_id: str = "session-1",
    account: str = ACCOUNT,
    *,
    purpose: str = PURPOSE,
    service: str = SERVICE_DOMAIN,
    expected_revision: int | None = None,
) -> bytes:
    return store.read(
        record_id,
        purpose=purpose,
        account_domain=ACCOUNT_DOMAIN,
        service_domain=service,
        account=account,
        expected_revision=expected_revision,
    )


def test_header_matches_frozen_schema_and_round_trips(secure):
    _, _, store, _ = secure
    assert write_session(store) == 1
    raw = (store.records_dir / "session-1.secure").read_bytes()
    header = parse_envelope(raw).header
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(header)
    assert header["magic"] == "THU-SECURE-1\0"
    assert header["format_version"] == 1
    assert header["cipher_suite"] == "AES-256-GCM"
    assert header["backend"] == key_backend()
    assert read_session(store) == b"synthetic-session"


def test_same_record_gets_fresh_nonce_and_ciphertext(secure):
    _, _, store, _ = secure
    assert write_session(store) == 1
    first = (store.records_dir / "session-1.secure").read_bytes()
    assert write_session(store) == 2
    second = (store.records_dir / "session-1.secure").read_bytes()
    assert first != second
    assert parse_envelope(first).header["nonce"] != parse_envelope(second).header["nonce"]
    assert read_session(store) == b"synthetic-session"


def test_tampered_ciphertext_and_nonce_fail_closed(secure):
    _, _, store, _ = secure
    write_session(store)
    path = store.records_dir / "session-1.secure"
    raw = path.read_bytes()
    envelope = parse_envelope(raw)

    broken = bytearray(envelope.ciphertext)
    broken[0] ^= 1
    path.write_bytes(pack_envelope(envelope.header, bytes(broken)))
    with pytest.raises(SecureEnvelopeCorrupt) as exc:
        read_session(store)
    assert b"synthetic-session" not in repr(exc.value).encode()

    header = dict(envelope.header)
    header["nonce"] = base64.urlsafe_b64encode(b"x" * 12).decode().rstrip("=")
    path.write_bytes(pack_envelope(header, envelope.ciphertext))
    with pytest.raises(SecureDecryptError):
        read_session(store)


def test_scope_mismatch_is_not_a_successful_read(secure):
    _, _, store, _ = secure
    write_session(store)
    with pytest.raises(SecureAccountMismatch):
        read_session(store, account="another-synthetic-user")
    with pytest.raises(SecurePurposeMismatch):
        read_session(store, purpose="ACTION")
    with pytest.raises(SecureServiceMismatch):
        read_session(store, service="INFO")


def test_tampered_header_scope_without_aad_fails(secure):
    _, _, store, _ = secure
    write_session(store)
    path = store.records_dir / "session-1.secure"
    envelope = parse_envelope(path.read_bytes())
    header = dict(envelope.header)
    header["service_domain"] = "INFO"
    path.write_bytes(pack_envelope(header, envelope.ciphertext))
    with pytest.raises(SecureEnvelopeCorrupt):
        read_session(store)


def test_revision_conflict_does_not_replace_newer_record(secure):
    _, _, store, _ = secure
    write_session(store)
    before = (store.records_dir / "session-1.secure").read_bytes()
    with pytest.raises(SecureRevisionConflict):
        store.write(
            "session-1",
            b"stale",
            purpose=PURPOSE,
            account_domain=ACCOUNT_DOMAIN,
            service_domain=SERVICE_DOMAIN,
            account=ACCOUNT,
            expected_revision=99,
        )
    assert (store.records_dir / "session-1.secure").read_bytes() == before
    assert write_session(store, value=b"new") == 2
    assert read_session(store) == b"new"


def test_atomic_write_failure_keeps_previous_record(secure, monkeypatch):
    _, _, store, _ = secure
    write_session(store)
    path = store.records_dir / "session-1.secure"
    before = path.read_bytes()

    def fail_replace(source, target):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(store_module, "_replace", fail_replace)
    with pytest.raises(SecureWriteError):
        write_session(store, value=b"replacement")
    assert path.read_bytes() == before
    assert not list(store.data_dir.rglob("*.tmp"))
    assert read_session(store) == b"synthetic-session"


def test_fsync_failure_keeps_previous_record(secure, monkeypatch):
    _, _, store, _ = secure
    write_session(store)
    path = store.records_dir / "session-1.secure"
    before = path.read_bytes()

    def fail_fsync(fd):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(store_module, "_fsync_file", fail_fsync)
    with pytest.raises(SecureWriteError):
        write_session(store, value=b"replacement")
    assert path.read_bytes() == before
    assert read_session(store) == b"synthetic-session"


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner/mode checks")
def test_missing_broad_and_linked_posix_keys_are_rejected(tmp_path):
    missing = tmp_path / "keys" / "missing.key"
    missing.parent.mkdir(mode=0o700)
    with pytest.raises(SecureKeyNotConfigured):
        load_provider(missing)

    key = tmp_path / "keys" / "broad.key"
    create_key_file(key, "file-key")
    key.chmod(0o644)
    with pytest.raises(SecureKeyPermissionError):
        load_provider(key, "file-key")
    key.chmod(0o600)

    link = tmp_path / "keys" / "link.key"
    link.symlink_to(key)
    with pytest.raises(SecureKeyPermissionError):
        load_provider(link, "file-key")

    world = tmp_path / "world"
    world.mkdir(mode=0o700)
    world.chmod(0o777)
    copied = world / "copied.key"
    shutil.copyfile(key, copied)
    copied.chmod(0o600)
    with pytest.raises(SecureKeyPermissionError):
        load_provider(copied, "file-key")


def test_key_must_live_outside_the_data_directory(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    key = data / "master.key"
    create_key_file(key, key_backend())
    provider = load_provider(key)
    with pytest.raises(SecureKeyLocationError):
        SecureStore(provider, data)


def test_record_identifier_cannot_escape_directory(secure):
    _, _, store, _ = secure
    with pytest.raises(SecureInvalidRecordId):
        store.write(
            "../escape",
            b"x",
            purpose=PURPOSE,
            account_domain=ACCOUNT_DOMAIN,
            service_domain=SERVICE_DOMAIN,
            account=ACCOUNT,
        )


def test_rotation_switches_atomically_and_rollback_recovers(tmp_path):
    old_key = tmp_path / "keys" / "old.key"
    new_key = tmp_path / "keys" / "new.key"
    create_key_file(old_key, key_backend())
    create_key_file(new_key, key_backend())
    old_provider = load_provider(old_key)
    new_provider = load_provider(new_key)
    store = SecureStore(old_provider, tmp_path / "data")
    write_session(store, "r1", b"one")
    store.write(
        "r2",
        b"two",
        purpose="ACTION",
        account_domain=ACCOUNT_DOMAIN,
        service_domain="LIBRARY",
        account=ACCOUNT,
    )
    before = store.inspect("r1")["account_ref"]
    preview = store.preview_rotation(new_provider)
    assert preview["records"] == 2
    assert preview["active_key_id"] == old_provider.key_id
    assert preview["target_key_id"] == new_provider.key_id

    result = store.rotate(new_provider)
    assert result["migrated"] == 2
    assert store.health()["active_key_id"] == new_provider.key_id
    assert store.inspect("r1")["account_ref"] == before
    assert read_session(store, "r1") == b"one"
    assert store.read(
        "r2",
        purpose="ACTION",
        account_domain=ACCOUNT_DOMAIN,
        service_domain="LIBRARY",
        account=ACCOUNT,
    ) == b"two"
    restarted = SecureStore(new_provider, tmp_path / "data", retired_providers=(old_provider,))
    assert read_session(restarted, "r1") == b"one"
    assert restarted.inspect("r1")["account_ref"] == before

    assert store.rollback()["active_key_id"] == old_provider.key_id
    assert read_session(store, "r1") == b"one"
    assert store.provider.key_id == old_provider.key_id


def test_rotation_half_failure_keeps_old_pointer_and_key(tmp_path, monkeypatch):
    old_key = tmp_path / "keys" / "old.key"
    new_key = tmp_path / "keys" / "new.key"
    create_key_file(old_key, key_backend())
    create_key_file(new_key, key_backend())
    old_provider = load_provider(old_key)
    new_provider = load_provider(new_key)
    store = SecureStore(old_provider, tmp_path / "data")
    write_session(store, "r1", b"one")
    write_session(store, "r2", b"two")
    original_seal = new_provider.seal
    calls = {"count": 0}

    def fail_second(data, context):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("synthetic rotation failure")
        return original_seal(data, context)

    monkeypatch.setattr(new_provider, "seal", fail_second)
    with pytest.raises(SecureRotationIncomplete) as exc:
        store.rotate(new_provider)
    assert exc.value.migrated == 1
    assert exc.value.total == 2
    assert store._pointer["active_key_id"] == old_provider.key_id
    assert read_session(store, "r1") == b"one"
    assert read_session(store, "r2") == b"two"

    monkeypatch.setattr(new_provider, "seal", original_seal)
    assert store.rotate(new_provider)["migrated"] == 2
    assert read_session(store, "r1") == b"one"


def test_pointer_switch_failure_keeps_old_pointer(tmp_path, monkeypatch):
    old_key = tmp_path / "keys" / "old.key"
    new_key = tmp_path / "keys" / "new.key"
    create_key_file(old_key, key_backend())
    create_key_file(new_key, key_backend())
    old_provider = load_provider(old_key)
    new_provider = load_provider(new_key)
    store = SecureStore(old_provider, tmp_path / "data")
    write_session(store)
    original_atomic = store_module._atomic_write

    def fail_pointer(path, data):
        if path.name == "store.json":
            raise SecureWriteError("synthetic pointer failure")
        return original_atomic(path, data)

    monkeypatch.setattr(store_module, "_atomic_write", fail_pointer)
    with pytest.raises(SecureRotationIncomplete):
        store.rotate(new_provider)
    assert store._pointer["active_key_id"] == old_provider.key_id
    assert read_session(store) == b"synthetic-session"


def test_store_contains_no_plaintext_or_temporary_artifacts(secure):
    _, _, store, data = secure
    secret = b"synthetic-private-business-value"
    write_session(store, "private", secret)
    for path in data.rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes()
            assert not path.name.endswith(".tmp")
    rendered = json.dumps(store.health(), ensure_ascii=False)
    assert secret.decode() not in rendered
    assert str(data) not in rendered


def test_production_entry_refuses_missing_key_and_does_not_use_test_codec(tmp_path):
    with pytest.raises(SecureKeyNotConfigured):
        provider_from_environment({})
    with pytest.raises(SecureKeyNotConfigured):
        open_store_from_environment({})

    key = tmp_path / "keys" / "master.key"
    create_key_file(key, key_backend())
    env = {
        "THU_SECURE_KEY_FILE": str(key),
        "THU_SECURE_DATA_DIR": str(tmp_path / "data"),
    }
    store = open_store_from_environment(env)
    assert store.provider.key_id == load_provider(key).key_id
    assert "tests.synthetic_store" not in sys.modules


def test_bridge_is_bounded_and_keeps_key_path_out_of_payload(tmp_path):
    key = tmp_path / "keys" / "master.key"
    create_key_file(key, key_backend())
    data = tmp_path / "data"
    env = subprocess_environment(key, data, base={"PATH": os.environ.get("PATH", "")})
    assert set(env) <= {
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "COMSPEC",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "USERNAME",
        "USERDOMAIN",
        "PYTHONPATH",
        "PYTHONUTF8",
        "PYTHONNOUSERSITE",
        "PSModulePath",
        "THU_SECURE_KEY_FILE",
        "THU_SECURE_DATA_DIR",
        "THU_SECURE_BACKEND",
    }
    plaintext = b"synthetic-bridge"
    response = handle_request(
        {
            "action": "seal",
            "purpose": PURPOSE,
            "account_domain": ACCOUNT_DOMAIN,
            "service_domain": SERVICE_DOMAIN,
            "account": ACCOUNT,
            "plaintext_b64": base64.b64encode(plaintext).decode(),
        },
        env,
    )
    assert response["ok"] is True
    assert str(key) not in json.dumps(response)
    envelope = base64.b64decode(response["envelope_b64"])
    unsealed = handle_request(
        {
            "action": "unseal",
            "purpose": PURPOSE,
            "account_domain": ACCOUNT_DOMAIN,
            "service_domain": SERVICE_DOMAIN,
            "account": ACCOUNT,
            "envelope_b64": base64.b64encode(envelope).decode(),
        },
        env,
    )
    assert unsealed["ok"] is True
    assert base64.b64decode(unsealed["plaintext_b64"]) == plaintext

    too_large = handle_request(
        {
            "action": "seal",
            "purpose": PURPOSE,
            "account_domain": ACCOUNT_DOMAIN,
            "service_domain": SERVICE_DOMAIN,
            "account": ACCOUNT,
            "plaintext_b64": base64.b64encode(
                b"x" * (bridge_module.MAX_PLAINTEXT_BYTES + 1)
            ).decode(),
        },
        env,
    )
    assert too_large["ok"] is False
    assert too_large["error"]["code"] == "SECURE_INVALID_INPUT"


def test_bridge_subprocess_has_a_single_json_response(tmp_path):
    key = tmp_path / "keys" / "master.key"
    create_key_file(key, key_backend())
    data = tmp_path / "data"
    env = subprocess_environment(key, data, base={"PATH": os.environ.get("PATH", "")})
    request = json.dumps({"action": "health"}).encode()
    result = subprocess.run(
        [sys.executable, "-m", "thu_assistant.secure_store.bridge"],
        input=request,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout.decode())
    assert payload["ok"] is True
    assert payload["health"]["key_id"]
    assert str(key) not in result.stdout.decode()


def test_record_missing_is_an_error_not_empty_data(secure):
    _, _, store, _ = secure
    with pytest.raises(Exception) as exc:
        read_session(store, "absent")
    assert type(exc.value).__name__ == "SecureRecordNotFound"


def test_health_detects_corrupt_record(secure):
    _, _, store, _ = secure
    write_session(store, "r1")
    path = store.records_dir / "r1.secure"
    path.write_bytes(b"not an envelope")
    health = store.health()
    assert health["ok"] is False
    assert health["corrupt_records"] == 1


def test_concurrent_writers_cannot_both_advance_the_same_revision(secure, tmp_path):
    key_path, _, store, data_dir = secure
    write_session(store, "r1", b"base")
    worker = textwrap.dedent(
        """
        import json, sys, time
        from thu_assistant.secure_store import SecureStore, load_provider
        key, data, value = sys.argv[1:4]
        time.sleep(0.15)
        try:
            store = SecureStore(load_provider(key), data)
            revision = store.write(
                "r1", value.encode(),
                purpose="SESSION", account_domain="CAMPUS",
                service_domain="LEARN", account="synthetic-student",
                expected_revision=1,
            )
            print(json.dumps({"ok": True, "revision": revision}))
        except Exception as exc:
            print(json.dumps({"ok": False, "code": getattr(exc, "code", type(exc).__name__)}))
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(key_path), str(data_dir), value],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            cwd=ROOT,
        )
        for value in ("first", "second")
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert sum(1 for result in results if result["ok"]) == 1
    assert any(result.get("code") == "SECURE_REVISION_CONFLICT" for result in results)
    assert read_session(store, "r1") in (b"first", b"second")


def test_legacy_format_is_identified_but_not_read_on_posix(tmp_path):
    from thu_assistant.secure_store import LegacyDpapiReader

    path = tmp_path / "legacy.dpapi"
    path.write_bytes(b"LA-DPAPI-1\0synthetic-ciphertext")
    assert LegacyDpapiReader.identify(path.read_bytes()) == "SESSION"
    if os.name != "nt":
        with pytest.raises(SecureLegacyUnavailable):
            LegacyDpapiReader(path).read()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI only")
def test_windows_dpapi_key_round_trip(tmp_path):
    key = tmp_path / "keys" / "master.key"
    create_key_file(key, "windows-dpapi")
    provider = load_provider(key, "windows-dpapi")
    store = SecureStore(provider, tmp_path / "data")
    write_session(store)
    assert read_session(store) == b"synthetic-session"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL only")
def test_windows_broad_key_acl_is_rejected(tmp_path):
    key = tmp_path / "keys" / "master.key"
    create_key_file(key, "windows-dpapi")
    result = subprocess.run(
        ["icacls", str(key), "/grant", "*S-1-1-0:F"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0
    with pytest.raises(SecureKeyPermissionError):
        load_provider(key, "windows-dpapi")


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI only")
def test_windows_legacy_record_is_readable_without_rewrite(tmp_path):
    from learn_assistant.auth import _dpapi_protect
    from thu_assistant.secure_store import LegacyDpapiReader

    payload = b'{"version":1,"synthetic":true}'
    protected = _dpapi_protect(payload)
    assert protected is not None
    path = tmp_path / "legacy.dpapi"
    path.write_bytes(b"LA-DPAPI-1\0" + protected)
    before = path.read_bytes()
    record = LegacyDpapiReader(path).read()
    assert record.format == "SESSION"
    assert record.plaintext == payload
    assert path.read_bytes() == before
