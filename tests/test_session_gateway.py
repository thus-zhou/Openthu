from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from thu_assistant.bridge import CampusError, GatewayClient
from thu_assistant.gateway_client import gateway_environment
from thu_assistant.secure_store.envelope import inspect_envelope
from thu_assistant.secure_store.keys import create_key_file
from thu_assistant.secure_store.providers import load_provider

ROOT = Path(__file__).parent.parent
NODE = os.environ.get("THU_NODE", "node")
GATEWAY = ROOT / "src/thu_assistant/gateway.js"
FIXTURE = ROOT / "tests/fixtures/gateway"
LOCK_MODULE = ROOT / "vendor/madmodel-proxy/platform/process-lock.js"
ACCOUNT = "synthetic-user"
ACCOUNT_HASH = hashlib.sha256(ACCOUNT.encode()).hexdigest()


def make_key(tmp_path: Path) -> Path:
    directory = tmp_path / "keys"
    directory.mkdir(mode=0o700)
    key = directory / "master.key"
    create_key_file(key)
    return key


def gateway_env(tmp_path: Path, *, key: Path | None = None, allow_legacy: bool = False) -> dict[str, str]:
    state = tmp_path / "state"
    campus = state / "campus"
    campus.mkdir(parents=True)
    env = {
        **os.environ,
        "THU_ACCESS_HELPER": str(FIXTURE / "helper.js"),
        "THU_MADMODEL_DIR": str(FIXTURE),
        "THU_ASSISTANT_STATE_DIR": str(campus),
        "MADMODEL_STATE_DIR": str(state),
        "THU_TEST_LOCK_MODULE": str(LOCK_MODULE),
        "THU_PYTHON": sys.executable,
        "PYTHONPATH": str(ROOT / "src"),
    }
    for name in ("THU_SECURE_KEY_FILE", "OPENTHU_SECURE_KEY_FILE", "THU_SECURE_BACKEND",
                 "OPENTHU_SECURE_BACKEND", "THU_SESSION_ALLOW_LEGACY"):
        env.pop(name, None)
    if key is not None:
        env["THU_SECURE_KEY_FILE"] = str(key)
        env["THU_SECURE_DATA_DIR"] = str(campus)
        env["THU_SECURE_BACKEND"] = "windows-dpapi" if os.name == "nt" else "file-key"
    if allow_legacy:
        env["THU_SESSION_ALLOW_LEGACY"] = "1"
    return env


def legacy_ciphertext(revision: int = 0, cookies: list | None = None) -> str:
    return base64.b64encode(json.dumps({
        "version": 2, "account": ACCOUNT_HASH, "revision": revision,
        "cookies": cookies or [], "apps": {},
    }).encode()).decode()


def unseal_session(path: Path, key: Path, revision: int, account_ref: str) -> dict:
    provider = load_provider(str(key), None)
    from thu_assistant.secure_store.envelope import StorageContext

    opened = provider.unseal(
        path.read_bytes(),
        StorageContext(
            purpose="SESSION", account_domain="CAMPUS", service_domain="LOCAL",
            account_ref=account_ref, revision=revision, record_id="campus-session",
        ),
        expected_revision=revision,
    )
    return json.loads(opened.plaintext)


def test_secure_session_migrates_legacy_and_retains_old_ciphertext(tmp_path):
    key = make_key(tmp_path)
    env = gateway_env(tmp_path, key=key)
    campus = Path(env["THU_ASSISTANT_STATE_DIR"])
    old = legacy_ciphertext()
    (campus / "campus-v2.dpapi").write_text(old)
    client = GatewayClient([NODE, str(GATEWAY), "--stdio"], env)
    try:
        result = client.call("read", url="https://info.tsinghua.edu.cn/", via_webvpn=True)
        assert result["status"] == "PAGE_READ_VERIFIED"
    finally:
        client.close()
    assert (campus / "campus-v2.dpapi").read_text() == old
    assert (campus / "campus-v2.dpapi.before-v3").read_text() == old
    secure = campus / "campus-v3.secure"
    assert secure.read_bytes().startswith(b"THU-SECURE-1\0")
    header = inspect_envelope(secure.read_bytes())
    assert header["purpose"] == "SESSION"
    assert header["service_domain"] == "LOCAL"
    assert header["record_id"] == "campus-session"
    assert header["revision"] >= 1
    state = unseal_session(secure, key, header["revision"], header["account_ref"])
    assert state["version"] == 3
    assert state["revision"] == header["revision"]
    assert state["migration"]["source"] == "LEGACY_DPAPI"
    assert state["migration"]["saved"] is True
    assert state["migration"]["verified"] is True
    assert state["migration"]["legacyRetained"] is True


@pytest.mark.skipif(os.name == "nt", reason="Windows preserves the legacy DPAPI path when no new key is configured")
def test_linux_missing_key_blocks_protected_session_but_not_catalog_or_public_read(tmp_path, monkeypatch):
    env = gateway_env(tmp_path)
    client = GatewayClient([NODE, str(GATEWAY), "--stdio"], env)
    try:
        with pytest.raises(CampusError, match="SECURE_KEY_NOT_CONFIGURED"):
            client.call("read", url="https://info.tsinghua.edu.cn/", via_webvpn=True)
        assert not (Path(env["THU_ASSISTANT_STATE_DIR"]) / "campus-v3.secure").exists()
        public = client.call("read", url="https://info.tsinghua.edu.cn/public", authenticate=False)
        assert public["public_read"] is True
    finally:
        client.close()
    monkeypatch.setenv("THU_ASSISTANT_STATE_DIR", env["THU_ASSISTANT_STATE_DIR"])
    from thu_assistant import catalog

    rows = catalog.list_services()
    assert rows["status"] == "LOCAL_CATALOG_READ"
    assert rows["count"] > 0


def test_browser_state_is_sealed_and_roundtrips_without_plaintext_on_stdout(tmp_path):
    key = make_key(tmp_path)
    env = gateway_env(tmp_path, key=key)
    client = GatewayClient([NODE, str(GATEWAY), "--stdio"], env)
    try:
        result = client.call("browser_state", url="https://info.tsinghua.edu.cn/")
        sealed = result["sealedState"]
        raw = base64.b64decode(sealed)
        assert raw.startswith(b"THU-SECURE-1\0")
        assert b"fixture-cookie" not in raw
        unsealed = client.call("unseal_browser_state", sealedState=sealed)
        assert unsealed["stateFormat"] == "secure-storage-header-v1"
        assert unsealed["state"]["serviceOrigin"] == "https://info.tsinghua.edu.cn"
        assert unsealed["state"]["cookies"]
        imported = client.call("import_browser_state", state={
            "cookies": unsealed["state"]["cookies"],
            "origins": unsealed["state"]["origins"],
            "syncToken": sealed,
        })
        assert imported["status"] == "BROWSER_SESSION_ENCRYPTED"
        assert imported["syncToken"] != sealed
        assert "fixture-cookie" not in json.dumps(result)
    finally:
        client.close()
    # The private gateway pipe contains ciphertext only, never a cookie value.
    request = {"type": "request", "request_id": "one", "timeout_ms": 10000,
               "command": {"command": "browser_state", "url": "https://info.tsinghua.edu.cn/"}}
    done = subprocess.run([NODE, str(GATEWAY), "--stdio"], input=json.dumps(request) + "\n",
                          capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0
    assert "fixture-cookie" not in done.stdout


def test_gateway_environment_uses_frozen_paths_and_rejects_a_relative_key(tmp_path):
    base = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    if os.name == "nt":
        base.update({"APPDATA": str(tmp_path / "AppData/Roaming"),
                     "LOCALAPPDATA": str(tmp_path / "AppData/Local")})
        config_base = tmp_path / "AppData/Roaming"
    else:
        config_base = tmp_path / ".config"
        base["XDG_CONFIG_HOME"] = str(config_base)
    env = gateway_environment(base)
    assert Path(env["THU_ASSISTANT_STATE_DIR"]).is_absolute()
    assert "THU_SECURE_KEY_FILE" not in env
    default_key = config_base / ("Openthu" if os.name == "nt" else "openthu") / "keys/master.key"
    default_key.parent.mkdir(parents=True, mode=0o700)
    create_key_file(default_key)
    env = gateway_environment(base)
    assert env["THU_SECURE_KEY_FILE"] == str(default_key)
    with pytest.raises(CampusError, match="SECURE_KEY_LOCATION"):
        gateway_environment({**base, "OPENTHU_SECURE_KEY_FILE": "relative.key"})


def test_crypto_service_echoes_ids_and_rejects_path_selectors(tmp_path):
    key = make_key(tmp_path)
    data = tmp_path / "state"
    data.mkdir(mode=0o700)
    env = {
        **os.environ,
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "src"),
        "THU_SECURE_KEY_FILE": str(key),
        "THU_SECURE_DATA_DIR": str(data),
        "THU_SECURE_BACKEND": "windows-dpapi" if os.name == "nt" else "file-key",
        "PYTHONUTF8": "1",
    }
    lines = [
        {"id": "one", "action": "health"},
        {"id": "two", "action": "health"},
        {"id": "three", "action": "seal", "path": "/tmp/other"},
    ]
    done = subprocess.run([sys.executable, "-m", "thu_assistant.crypto_service"],
                          input="".join(json.dumps(row) + "\n" for row in lines),
                          capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0
    responses = [json.loads(line) for line in done.stdout.splitlines()]
    assert [row["id"] for row in responses] == ["one", "two", "three"]
    assert responses[0]["ok"] is True
    assert responses[2]["ok"] is False
    assert responses[2]["error"]["code"] == "SECURE_INVALID_INPUT"


def test_learn_session_and_action_journal_use_the_secure_store_and_fail_closed(tmp_path, monkeypatch):
    key = make_key(tmp_path)
    learn = tmp_path / "learn"
    monkeypatch.setenv("THU_SECURE_KEY_FILE", str(key))
    monkeypatch.setenv("OPENTHU_LEARN_SESSION_DIR", str(learn))
    from learn_assistant.auth import SecureSessionStore, _dpapi_protect, _dpapi_unprotect
    from learn_assistant.models import BrowserCookie, BrowserSession

    session = BrowserSession(
        cookies=(BrowserCookie(name="sid", value="synthetic", domain=".learn.tsinghua.edu.cn",
                               path="/", secure=True, expires=None),),
        user_agent="synthetic", csrf_token="csrf", captured_at="now", source="synthetic",
    )
    store = SecureSessionStore()
    assert store.save(session) is True
    assert store.last_format == "secure-store"
    assert (learn / "session.secure").read_bytes().startswith(b"THU-SECURE-1\0")
    loaded = store.load()
    assert loaded is not None and loaded.cookies[0].value == "synthetic"
    envelope = _dpapi_protect(b"synthetic-action")
    assert envelope.startswith(b"THU-SECURE-1\0")
    assert _dpapi_unprotect(envelope) == b"synthetic-action"
    monkeypatch.delenv("THU_SECURE_KEY_FILE")
    monkeypatch.delenv("OPENTHU_SECURE_KEY_FILE", raising=False)
    import learn_assistant.auth as auth
    auth._SECURE_PROVIDERS.clear()
    if os.name != "nt":
        assert _dpapi_protect(b"synthetic-action") is None


@pytest.mark.parametrize("values,expected", [(("a=1", "b=2"), "saved"), (("a=1", "a=2"), "STATE_CONFLICT")])
def test_two_secure_processes_merge_or_reject_stale_cookies(tmp_path, values, expected):
    key = make_key(tmp_path)
    env = gateway_env(tmp_path, key=key)
    campus = Path(env["THU_ASSISTANT_STATE_DIR"])
    workers = [
        subprocess.Popen([NODE, str(ROOT / "tests/fixtures/session-writer.js")], env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for _ in range(2)
    ]
    try:
        assert [worker.stdout.readline().strip() for worker in workers] == ["ready", "ready"]
        for worker, value, result in zip(workers, values, ("saved", expected)):
            worker.stdin.write(value + "\n")
            worker.stdin.flush()
            assert worker.stdout.readline().strip() == result
            worker.wait(timeout=10)
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
    secure = campus / "campus-v3.secure"
    assert secure.read_bytes().startswith(b"THU-SECURE-1\0")
    header = inspect_envelope(secure.read_bytes())
    state = unseal_session(secure, key, header["revision"], header["account_ref"])
    assert len(state["cookies"]) == (2 if expected == "saved" else 1)
    assert state["cookies"][0]["value"] == "1"
