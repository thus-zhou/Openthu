"""Temporary Windows ACL diagnostic. No paths or account values are printed."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from thu_assistant.secure_store import keys


def clean(value: str) -> str:
    text = value or ""
    for marker in (str(root), os.environ.get("USERPROFILE", ""), os.environ.get("TEMP", "")):
        if marker:
            text = text.replace(marker, "<local>")
    return text.replace("\r", "\\r")[:300]


def run(label: str, argv, *, timeout: int = 8):
    env = keys._minimal_windows_env()
    env["OPENTHU_ACL_PATH"] = str(target)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        print(label, "TIMEOUT", timeout)
        print(label, "partial_out", clean((exc.stdout or "") if isinstance(exc.stdout, str) else ""))
        print(label, "partial_err", clean((exc.stderr or "") if isinstance(exc.stderr, str) else ""))
        return None
    except Exception as exc:
        print(label, "ERROR", type(exc).__name__, clean(str(exc)))
        return None
    print(label, "rc", result.returncode)
    print(label, "stdout", clean(result.stdout))
    print(label, "stderr", clean(result.stderr))
    return result


root = Path(tempfile.mkdtemp(prefix="openthu-acl-"))
(root / "keys").mkdir()
target = root / "keys" / "master.key"
target.write_bytes(b"x" * 42)
print("env_keys", sorted(keys._minimal_windows_env()))
print("where_powershell", shutil.which("powershell"))
print("where_pwsh", shutil.which("pwsh"))
print("windows_powershell_exists", Path(keys._powershell()).is_file())
print("pwsh7_exists", Path(r"C:\Program Files\PowerShell\7\pwsh.exe").is_file())

run("powershell_hi", [keys._powershell(), "-NoProfile", "-NonInteractive", "-Command", "Write-Output hi"], timeout=8)
run("pwsh_hi", [r"C:\Program Files\PowerShell\7\pwsh.exe", "-NoProfile", "-NonInteractive", "-Command", "Write-Output hi"], timeout=8)
run("icacls_help", ["icacls"], timeout=8)

scripts = {
    "getacl": (
        "Import-Module Microsoft.PowerShell.Security -ErrorAction Stop; "
        "[Console]::Out.Write(([System.Security.Principal.WindowsIdentity]::"
        "GetCurrent().User.Value) + \"`n\" + (Get-Acl -LiteralPath "
        "$env:OPENTHU_ACL_PATH -ErrorAction Stop).Sddl)"
    ),
    "getitem": (
        "[Console]::Out.Write(([System.Security.Principal.WindowsIdentity]::"
        "GetCurrent().User.Value) + \"`n\" + ((Get-Item -LiteralPath "
        "$env:OPENTHU_ACL_PATH).GetAccessControl()).Sddl)"
    ),
}
sid = ""
for label, script in scripts.items():
    result = run(label, [keys._powershell(), "-NoProfile", "-NonInteractive", "-Command", script], timeout=8)
    if result and result.stdout:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if lines:
            sid = lines[0]

if sid:
    run("inherit", ["icacls", str(target), "/inheritance:r"])
    run("grant", ["icacls", str(target), "/grant:r", f"*{sid}:F"])

for label, call in (
    ("snapshot", lambda: keys._windows_acl_snapshot(target)),
    ("harden", lambda: keys._harden_windows_acl(target)),
    ("owner_only", lambda: keys._windows_acl_owner_only(target)),
):
    try:
        value = call()
        print(label, "ok", str(value)[:120])
        if label == "snapshot":
            print("snapshot_sddl", clean(str(value[1])))
        if label == "harden":
            after = keys._windows_acl_snapshot(target)
            print("after_sddl", clean(str(after[1])))
    except Exception as exc:
        print(label, "error", type(exc).__name__, getattr(exc, "detail", ""))

secret = b"s" * 32
protected = keys._dpapi("protect", secret)
print("dpapi_protect", bool(protected), len(protected or b""))
restored = keys._dpapi("unprotect", protected or b"")
print("dpapi_roundtrip", restored == secret, len(restored or b""))

key = root / "keys" / "dpapi.key"
try:
    keys.create_key_file(key, "windows-dpapi")
    raw = key.read_bytes()
    print("key_size", len(raw))
    print("key_magic", raw[: len(keys.DPAPI_KEY_MAGIC)] == keys.DPAPI_KEY_MAGIC)
    payload = raw[len(keys.DPAPI_KEY_MAGIC) :]
    print("payload_size", len(payload))
    restored = keys._dpapi("unprotect", payload)
    print("payload_unprotect", restored is not None, len(restored or b""))
    material = keys.load_key_file(key, "windows-dpapi")
    print("load_key_ok", material.backend, len(material.secret))
except Exception as exc:
    print("key_error", type(exc).__name__, getattr(exc, "detail", ""))

manual_secret = os.urandom(32)
manual_protected = keys._dpapi("protect", manual_secret)
print("manual_protected", len(manual_protected or b""))
manual_path = root / "keys" / "manual.key"
manual_path.write_bytes(keys.DPAPI_KEY_MAGIC + (manual_protected or b""))
manual_raw = manual_path.read_bytes()
manual_payload = manual_raw[len(keys.DPAPI_KEY_MAGIC) :]
print("manual_sizes", len(manual_raw), len(manual_payload))
print("manual_same", manual_payload == manual_protected)
manual_restored = keys._dpapi("unprotect", manual_payload)
print("manual_unprotect", manual_restored == manual_secret)
try:
    manual_material = keys.load_key_file(manual_path, "windows-dpapi")
    print("manual_load_ok", len(manual_material.secret))
except Exception as exc:
    print("manual_load_error", type(exc).__name__, getattr(exc, "detail", ""))

for index in range(20):
    direct_secret = os.urandom(32)
    direct_protected = keys._dpapi("protect", direct_secret)
    direct_restored = keys._dpapi("unprotect", direct_protected or b"")
    print(
        "direct",
        index,
        len(direct_protected or b""),
        direct_restored == direct_secret,
        direct_secret.count(0),
    )

for index in range(12):
    loop_key = root / "keys" / f"loop-{index}.key"
    try:
        original_write_all = keys._write_all
        original_harden = keys._harden_windows_acl
        last_payload = {"data": b""}
        def spy_write_all(fd, data, _original=original_write_all):
            last_payload["data"] = data
            print("write_all_len", len(data))
            return _original(fd, data)
        def spy_harden(path, _original=original_harden):
            print("before_harden_size", len(path.read_bytes()))
            result = _original(path)
            print("after_harden_size", len(path.read_bytes()))
            return result
        keys._write_all = spy_write_all
        keys._harden_windows_acl = spy_harden
        keys.create_key_file(loop_key, "windows-dpapi")
        keys._write_all = original_write_all
        keys._harden_windows_acl = original_harden
        loop_raw = loop_key.read_bytes()
        loop_payload = loop_raw[len(keys.DPAPI_KEY_MAGIC) :]
        print(
            "prefix_eq",
            loop_raw[: len(last_payload["data"])] == last_payload["data"],
            "extra",
            loop_raw[len(last_payload["data"]) :].hex(),
        )
        loop_restored = keys._dpapi("unprotect", loop_payload)
        print(
            "loop",
            index,
            len(loop_raw),
            len(loop_payload),
            loop_restored is not None,
            len(loop_restored or b""),
        )
    except Exception as exc:
        print("loop_error", index, type(exc).__name__, getattr(exc, "detail", ""))

manual_secret = os.urandom(32)
manual_protected = keys._dpapi("protect", manual_secret)
manual_payload = keys.DPAPI_KEY_MAGIC + (manual_protected or b"")
manual_file = root / "keys" / "manual-harden.key"
fd = os.open(manual_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    keys._write_all(fd, manual_payload)
    os.fsync(fd)
finally:
    os.close(fd)
print("pre_harden_size", len(manual_file.read_bytes()))
keys._harden_windows_acl(manual_file)
print("post_harden_size", len(manual_file.read_bytes()))
print(
    "post_harden_unprotect",
    keys._dpapi("unprotect", manual_file.read_bytes()[len(keys.DPAPI_KEY_MAGIC) :])
    == manual_secret,
)

from thu_assistant.secure_store import bridge as bridge_module

bridge_key = root / "keys" / "bridge.key"
keys.create_key_file(bridge_key, "windows-dpapi")
bridge_env = bridge_module.subprocess_environment(
    bridge_key, root / "bridge-data", base={"PATH": os.environ.get("PATH", "")}
)
bridge_result = subprocess.run(
    [sys.executable, "-m", "thu_assistant.secure_store.bridge"],
    input=json.dumps({"action": "health"}).encode(),
    capture_output=True,
    env=bridge_env,
    timeout=30,
    check=False,
)
print("bridge_rc", bridge_result.returncode)
print("bridge_out", clean(bridge_result.stdout.decode("utf-8", "replace")))
print("bridge_err", clean(bridge_result.stderr.decode("utf-8", "replace")))

import traceback

try:
    bridge_module._provider_and_paths(bridge_env)
    print("bridge_inproc ok")
except Exception:
    print("bridge_inproc error")
    print(clean(traceback.format_exc()))
