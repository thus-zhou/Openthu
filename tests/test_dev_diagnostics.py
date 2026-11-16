"""First-run diagnostics: complete and missing synthetic environments."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_diagnostics():
    spec = importlib.util.spec_from_file_location(
        "dev_diagnostics", ROOT / "scripts" / "dev_diagnostics.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    return {
        "root": root,
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
    assert rows["python"]["status"] == "ok"
    assert rows["python-deps"]["status"] == "ok"
    assert rows["node"]["status"] == "ok"
    assert rows["npm"]["status"] == "ok"
    assert rows["node-runtime"]["status"] == "ok"
    assert rows["playwright-browser"]["status"] == "ok"
    assert rows["source-package"]["status"] == "ok"
    assert rows["windows-auth"]["status"] == "limited"
    assert rows["windows-auth"]["required"] is False
    text = json.dumps(report, ensure_ascii=False)
    assert str(root) not in text
    assert str(tmp_path) not in text


def test_missing_dependencies_block_with_actionable_fixes(tmp_path):
    module = load_diagnostics()
    root = tmp_path / "empty"
    root.mkdir()
    report = module.collect(
        root=root,
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
        "playwright-browser",
    ):
        assert rows[check_id]["status"] in ("missing", "outdated")
        assert rows[check_id].get("fix")
    assert rows["source-package"]["status"] == "warning"
    assert rows["source-package"]["required"] is False
    assert report["summary"]["blocked"] >= 6


def test_lockfile_mismatch_points_to_setup_node(tmp_path):
    module = load_diagnostics()
    root = complete_checkout(tmp_path, installed="5.0.0")
    report = module.collect(**healthy_kwargs(root))
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["node-runtime"]["status"] == "outdated"
    assert "setup-node" in rows["node-runtime"]["fix"]
    assert report["ok"] is False


def test_windows_checkout_requires_complete_auth_helper(tmp_path):
    module = load_diagnostics()
    root = complete_checkout(tmp_path, with_windows=False)
    report = module.collect(**healthy_kwargs(root, platform_name="nt"))
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["windows-auth"]["status"] == "missing"
    assert "完整源码包" in rows["windows-auth"]["fix"]
    assert report["ok"] is False


def test_public_report_omits_host_account_and_absolute_paths(tmp_path):
    module = load_diagnostics()
    root = tmp_path / "Users" / "synthetic-person" / "Openthu"
    root.mkdir(parents=True)
    report = module.collect(
        root=root,
        platform_name="posix",
        which=lambda name: None,
        distribution_version=lambda name: None,
        browser_available=lambda: False,
        python_version=(3, 9, 18),
        python_environment="系统 Python",
        command_version=lambda path: None,
    )
    text = json.dumps(report, ensure_ascii=False)
    assert str(root) not in text
    assert str(Path.home()) not in text
    for key in ("hostname", "account_id", "password", "secret", "access_token"):
        assert key not in text.lower()


def test_dev_entry_advertises_doctor_without_importing_site_packages():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "dev.py"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "doctor" in result.stdout
