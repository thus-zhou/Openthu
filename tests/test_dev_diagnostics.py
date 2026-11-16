"""First-run diagnostics: complete and missing synthetic environments."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_diagnostics():
    spec = importlib.util.spec_from_file_location(
        "dev_diagnostics", ROOT / "scripts" / "dev_diagnostics.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_config(package_dir: Path):
    """A valid, path-free configuration facade for isolated diagnostics."""
    return SimpleNamespace(
        resources=SimpleNamespace(root=package_dir),
        public_dict=lambda: {"config_file": "defaults"},
    )


def complete_checkout(tmp_path, *, installed="6.0.0", with_node_modules=True, with_windows=True):
    root = tmp_path / "checkout"
    package = root / "src" / "thu_assistant"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"dependencies": {"tough-cookie": "6.0.0"}}), encoding="utf-8"
    )
    lock = {
        "lockfileVersion": 3,
        "packages": {"node_modules/tough-cookie": {"version": "6.0.0"}},
    }
    (package / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    for name in ("gateway.js", "security.js", "cookies.js", "models.js"):
        (package / name).write_text("// synthetic resource\n", encoding="utf-8")
    (package / "services.json").write_text(
        json.dumps([{"id": "synthetic-service", "name": "Synthetic"}]), encoding="utf-8"
    )
    if with_node_modules:
        installed_dir = package / "node_modules" / "tough-cookie"
        installed_dir.mkdir(parents=True)
        (installed_dir / "package.json").write_text(
            json.dumps({"version": installed}), encoding="utf-8"
        )
    if with_windows:
        (root / "runtime" / "windows").mkdir(parents=True)
        (root / "runtime" / "windows" / "access.js").write_text(
            "// synthetic helper\n", encoding="utf-8"
        )
        vendor = root / "vendor" / "madmodel-proxy"
        vendor.mkdir(parents=True)
        (vendor / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "dev.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    return root


def healthy_kwargs(root, *, platform_name="posix", node="v24.18.0", npm="11.16.0"):
    versions = {
        name: "1.0" for name in ("pytest", "requests", "playwright", "build", "openthu")
    }
    bins = {
        "node": "/synthetic/bin/node",
        "npm": "/synthetic/bin/npm",
        "npm.cmd": "/synthetic/bin/npm.cmd",
    }
    package = root / "src" / "thu_assistant"
    return {
        "root": root,
        "config": synthetic_config(package),
        "platform_name": platform_name,
        "which": lambda name: bins.get(name),
        "distribution_version": lambda name: versions.get(name),
        "browser_available": lambda: True,
        "python_version": (3, 12, 3),
        "python_environment": "虚拟环境",
        "command_version": lambda path: node if "node" in Path(path).name else npm,
    }


def test_complete_checkout_is_ready_and_contains_no_private_paths(tmp_path):
    module = load_diagnostics()
    root = complete_checkout(tmp_path)
    report = module.collect(**healthy_kwargs(root))
    assert report["ok"] is True
    rows = {row["id"]: row for row in report["checks"]}
    for check_id in (
        "python",
        "python-deps",
        "node",
        "npm",
        "node-runtime",
        "readonly-catalog",
        "playwright-browser",
        "source-package",
        "configuration",
        "storage-paths",
    ):
        assert rows[check_id]["status"] == "pass"
    assert rows["windows-auth"]["status"] == "not_applicable"
    assert rows["windows-auth"]["blocking"] is False
    text = json.dumps(report, ensure_ascii=False)
    assert str(root) not in text
    assert str(tmp_path) not in text


def test_missing_dependencies_block_with_actionable_fixes(tmp_path):
    module = load_diagnostics()
    root = tmp_path / "empty"
    root.mkdir()
    package = root / "src" / "thu_assistant"
    report = module.collect(
        root=root,
        config=synthetic_config(package),
        platform_name="posix",
        which=lambda name: None,
        distribution_version=lambda name: None,
        browser_available=lambda: False,
        python_version=(3, 9, 18),
        python_environment="系统 Python",
        command_version=lambda path: None,
    )
    assert report["ok"] is False
    rows = {row["id"]: row for row in report["checks"]}
    for check_id in (
        "python",
        "python-deps",
        "node",
        "npm",
        "node-runtime",
        "readonly-catalog",
    ):
        assert rows[check_id]["status"] == "fail"
        assert rows[check_id]["blocking"] is True
        assert rows[check_id].get("fix")
    assert rows["playwright-browser"]["status"] == "warning"
    assert rows["playwright-browser"]["blocking"] is False
    assert rows["source-package"]["status"] == "fail"
    assert rows["source-package"]["blocking"] is True
    assert report["summary"]["blocking"] >= 6


def test_lockfile_mismatch_points_to_setup_node(tmp_path):
    module = load_diagnostics()
    root = complete_checkout(tmp_path, installed="5.0.0")
    report = module.collect(**healthy_kwargs(root))
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["node-runtime"]["status"] == "fail"
    assert "setup-node" in rows["node-runtime"]["fix"]
    assert report["ok"] is False


def test_windows_checkout_requires_complete_auth_helper(tmp_path):
    module = load_diagnostics()
    root = complete_checkout(tmp_path, with_windows=False)
    report = module.collect(**healthy_kwargs(root, platform_name="nt"))
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["windows-auth"]["status"] == "fail"
    assert rows["windows-auth"]["blocking"] is True
    assert "完整源码包" in rows["windows-auth"]["fix"]
    assert report["ok"] is False


def test_public_report_omits_host_account_absolute_paths_and_command_output(tmp_path):
    module = load_diagnostics()
    root = tmp_path / "Users" / "synthetic-person" / "Openthu"
    root.mkdir(parents=True)
    malicious = "/home/synthetic-private/secret token=do-not-echo\n" + "x" * 10000
    report = module.collect(
        root=root,
        config=synthetic_config(root / "src" / "thu_assistant"),
        platform_name="posix",
        which=lambda name: "/synthetic/bin/" + name,
        distribution_version=lambda name: None,
        browser_available=lambda: False,
        python_version=(3, 12, 3),
        python_environment="虚拟环境",
        command_version=lambda path: malicious,
    )
    text = json.dumps(report, ensure_ascii=False)
    assert str(root) not in text
    assert str(Path.home()) not in text
    assert "synthetic-private" not in text
    assert "do-not-echo" not in text
    assert "x" * 100 not in text
    for key in ("hostname", "account_id", "password", "access_token"):
        assert key not in text.lower()


def test_invalid_configuration_is_reported_by_relative_item_only(tmp_path):
    module = load_diagnostics()
    root = complete_checkout(tmp_path)
    error = module.configuration_module.ConfigurationError(
        "CONFIG_INVALID_TYPE", "ui.page_size"
    )
    kwargs = healthy_kwargs(root)
    kwargs["config"] = None
    kwargs["config_error"] = error
    report = module.collect(**kwargs)
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["configuration"]["status"] == "fail"
    assert rows["configuration"]["blocking"] is True
    assert "ui.page_size" in rows["configuration"]["reason"]
    assert "CONFIG_INVALID_TYPE" in rows["configuration"]["reason"]
    assert rows["storage-paths"]["status"] == "not_applicable"
    assert report["ok"] is False


def test_lockfile_version_is_sanitized_and_never_crashes(tmp_path):
    module = load_diagnostics()
    for index, value in enumerate(("/home/synthetic-private/secret", 123)):
        package = tmp_path / f"lock-{index}"
        package.mkdir()
        (package / "package-lock.json").write_text(
            json.dumps(
                {"packages": {"node_modules/tough-cookie": {"version": value}}}
            ),
            encoding="utf-8",
        )
        row = module._node_runtime(package)
        assert row["status"] == "fail"
        assert row["available_version"] is None
        text = json.dumps(row, ensure_ascii=False)
        assert "synthetic-private" not in text
        assert "123" not in text


def test_timeout_is_reported_distinctly_from_a_version_problem(tmp_path):
    module = load_diagnostics()
    root = complete_checkout(tmp_path)
    kwargs = healthy_kwargs(root)
    kwargs["command_probe"] = lambda path: {"status": "timeout", "version": None}
    report = module.collect(**kwargs)
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["node"]["status"] == "fail"
    assert "超时" in rows["node"]["reason"]
    assert rows["npm"]["status"] == "warning"
    assert "超时" in rows["npm"]["reason"]
    assert report["ok"] is False


def test_configured_node_path_is_used_by_diagnostics(tmp_path):
    module = load_diagnostics()
    root = complete_checkout(tmp_path)
    package = root / "src" / "thu_assistant"
    configured_node = str(tmp_path / "configured-node.exe")
    config = synthetic_config(package)
    config.runtime = SimpleNamespace(
        node=configured_node, command_timeout_seconds=2, max_output_bytes=2048
    )
    seen = []

    def probe(path):
        seen.append(path)
        if path == configured_node:
            return {"status": "pass", "version": "24.18.0"}
        return {"status": "pass", "version": "11.16.0"}

    kwargs = healthy_kwargs(root)
    kwargs.update(
        config=config,
        command_probe=probe,
        which=lambda name: "/synthetic/" + name,
    )
    report = module.collect(**kwargs)
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["node"]["status"] == "pass"
    assert configured_node in seen


def test_missing_required_package_resources_block_doctor(tmp_path):
    module = load_diagnostics()
    root = complete_checkout(tmp_path)
    package = root / "src" / "thu_assistant"
    (package / "security.js").unlink()
    (package / "cookies.js").unlink()
    report = module.collect(**healthy_kwargs(root))
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["source-package"]["status"] == "fail"
    assert rows["source-package"]["blocking"] is True
    assert "security.js" in rows["source-package"]["reason"]
    assert report["ok"] is False


def test_dev_environment_clears_user_openthu_configuration(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "audit_dev", ROOT / "scripts" / "dev.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("OPENTHU_STATE_DIR", "/private/state")
    monkeypatch.setenv("OPENTHU_CONFIG_FILE", "/private/config.json")
    env = module.environment(tmp_path)
    assert "OPENTHU_STATE_DIR" not in env
    assert "OPENTHU_CONFIG_FILE" not in env
    assert env["OPENTHU_DOWNLOAD_DIR"] == str(tmp_path / "downloads")


def test_doctor_json_exposes_categories_and_stable_exit():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "dev.py"), "doctor", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1)
    payload = json.loads(result.stdout)
    assert set(payload) >= {"ok", "summary", "checks", "note"}
    capabilities = {row["capability"] for row in payload["checks"]}
    assert {
        "development",
        "readonly",
        "browser",
        "production-auth",
        "packaging",
        "config",
    } <= capabilities
    statuses = {row["status"] for row in payload["checks"]}
    assert statuses <= {"pass", "warning", "fail", "not_applicable"}
    assert (result.returncode == 0) == payload["ok"]


def test_executable_argv_quotes_windows_cmd_without_shell():
    module = load_diagnostics()
    argv = module.executable_argv(
        r"D:\Program Files\nodejs\npm.cmd",
        ["ci", "--registry=https://registry.npmjs.org"],
        platform_name="nt",
    )
    assert argv[0].lower().endswith("cmd.exe") or argv[0].lower().endswith("cmd")
    assert argv[1:3] == ["/d", "/s"]
    assert argv[3] == "/c"
    assert "npm.cmd" in argv[4]
    assert "Program Files" in argv[4]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell fixture")
def test_command_version_handles_an_executable_path_with_spaces(tmp_path):
    module = load_diagnostics()
    script = tmp_path / "fake node" / "node"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nprintf 'v24.18.0\\n'\n", encoding="utf-8")
    script.chmod(0o755)
    assert module._command_version(str(script)) == "24.18.0"


def test_dev_entry_advertises_doctor_and_config_without_site_packages():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "dev.py"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "doctor" in result.stdout
    assert "--config" in result.stdout
