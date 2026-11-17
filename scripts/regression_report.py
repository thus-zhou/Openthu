"""Version regression report, release gate and privacy aggregation (module 7.2).

The report is built from the JUnit XML pytest writes and from ``dev.py test``'s
Node summary.  It answers, in one machine-readable file, what source commit and
contracts the evidence belongs to, which platform produced it, how many cases
ran, passed, failed or skipped, why anything was skipped, which module a
failure belongs to, and the minimal command that reproduces it.

Safety rules enforced here:

* only a failure class and a repository-relative node id leave the process; raw
  exception text, absolute paths, tokens, cookies and account values never do;
* an unexplained skip fails the gate, an environment gap keeps the platform
  ``INCOMPLETE`` instead of claiming full availability, and platform-only skips
  stay explainable;
* the release gate scans the source tree and every candidate archive, audits
  the pinned dependencies, checks conservative defaults and verifies the
  candidate package before it can return PASS.

Nothing here contacts a campus service, a model endpoint or the internet.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform as platform_module
import re
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ElementTree
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

REPORT_SCHEMA = "version-regression-report-v1"
AGGREGATE_SCHEMA = "version-release-report-v1"
RELEASE_SCHEMA = "version-release-gate-v1"
CONTRACT_DIGEST_SCHEMA = "contract-inventory-v1"

EXPECTED_RUNTIME = {
    "cryptography": "50.0.2",
    "pillow": "13.0.0",
    "requests": "2.33.0",
}
EXPECTED_DEV = {
    "playwright": "1.57.0",
    "pytest": "8.3.4",
    "build": "1.2.2.post1",
    "jsonschema": "4.26.0",
    "wheel": "0.48.0",
}
EXPECTED_NODE = {"tough-cookie": "6.0.0"}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "loopback"}
DEFAULT_REQUIRED_PLATFORMS = ("linux", "windows")

# A test file belongs to exactly one module for failure attribution.  The map is
# deliberately explicit: a new test file that forgets to register lands in
# ``unmapped``, which the report shows instead of silently hiding it.
MODULE_BY_FILE = {
    "tests/test_academics.py": "4.1",
    "tests/test_action_framework.py": "4.4",
    "tests/test_actions.py": "4.4",
    "tests/test_auth_navigation.py": "3.1",
    "tests/test_batch3_integration.py": "batch-3",
    "tests/test_batch4_integration.py": "batch-4",
    "tests/test_batch5_integration.py": "batch-5",
    "tests/test_batch6_integration.py": "batch-6",
    "tests/test_calendar_export.py": "5.4",
    "tests/test_campus.py": "3.1",
    "tests/test_cli.py": "6.1",
    "tests/test_configuration.py": "2.1",
    "tests/test_dev_diagnostics.py": "2.1",
    "tests/test_distribution.py": "6.3",
    "tests/test_events.py": "5.4",
    "tests/test_facade.py": "6.1",
    "tests/test_facade_audit.py": "6.1",
    "tests/test_gateway_bridge.py": "3.1",
    "tests/test_learn_read.py": "3.1",
    "tests/test_library.py": "4.3",
    "tests/test_library_actions.py": "5.1",
    "tests/test_library_contracts.py": "4.3",
    "tests/test_library_renewal.py": "5.1",
    "tests/test_local_api.py": "6.2",
    "tests/test_media_assets.py": "3.3",
    "tests/test_model_media.py": "4.5",
    "tests/test_models.py": "4.5",
    "tests/test_mvp.py": "v0.1",
    "tests/test_plugin_contract.py": "6.1",
    "tests/test_queries.py": "3.2",
    "tests/test_query_contracts.py": "3.2",
    "tests/test_resources.py": "4.2",
    "tests/test_secure_store.py": "2.2",
    "tests/test_session_gateway.py": "3.1",
    "tests/test_source_contracts.py": "2.3",
    "tests/test_spaces.py": "4.3",
    "tests/test_spaces_contracts.py": "4.3",
    "tests/test_sports.py": "4.3",
    "tests/test_sports_actions.py": "5.2",
    "tests/test_sports_contracts.py": "4.3",
    "tests/test_submissions.py": "5.3",
    "tests/test_todos.py": "5.4",
    "tests/test_upgrade.py": "6.3",
    "tests/test_v020_contracts.py": "1.1",
    "tests/test_web_assets.py": "7.1",
    "tests/test_web_provider.py": "3.1",
}

# Exception text never becomes part of the report.  The first line is reduced to
# one of these stable classes (or ``Failure``) so a public summary can say what
# kind of failure happened without carrying a value.
_ERROR_CLASS = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]{0,80}(?:Error|Exception|Exit|Failed|Failure))(?::|$)")
_SAFE_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$")
_TOKEN_PATTERNS = (
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(?:set-)?cookie\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bticket\s*[:=]\s*[A-Za-z0-9._~+/=-]{8,}"),
)
_PRIVATE_PATH_PATTERNS = (
    re.compile(r"(?<![\w.-])/(?:home|Users|root|tmp|mnt|var/folders|private/var)/[^\s\"']*"),
    re.compile(r"(?i)\b[A-Za-z]:\\[^\s\"']*"),
    re.compile(r"\\\\[A-Za-z0-9._-]+\\[^\s\"']*"),
)
_PRIVATE_IP = re.compile(
    r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])"
)
_ACCOUNT_SCOPE = re.compile(r"\bacct_[A-Za-z0-9_-]{16,}\b")
_CREDENTIAL_FILES = {
    "creds.json", "credentials.json", "cookies.json", "session.json",
    "token.json", "auth.json", "hosts.yml",
}
MARKER_RULES = {
    "credential-token": _TOKEN_PATTERNS,
    "private-path": _PRIVATE_PATH_PATTERNS,
    "private-host": (_PRIVATE_IP,),
    "account-scope": (_ACCOUNT_SCOPE,),
}

ENVIRONMENT_SKIP_HINTS = (
    "node is unavailable", "node.js is not installed", "node is not installed",
    "playwright", "browser", "chromium", "screen", "display",
)
PLATFORM_SKIP_HINTS = (
    "windows", "posix", "non-windows", "platform", "symlink", "symbolic link",
    "ntfs", "pillow", "webp", "root can read", "descriptor accounting",
    "/proc", "posix shell", "zip modes",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Safe text
# ---------------------------------------------------------------------------

def sanitize_text(value: object, *, limit: int = 160) -> str:
    """Keep a bounded, printable summary with no token, path or private IP."""
    text = str(value or "")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 0x20)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    for pattern in _PRIVATE_PATH_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = _PRIVATE_IP.sub("[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def failure_class(message: object) -> str:
    text = sanitize_text(message, limit=200)
    found = _ERROR_CLASS.match(text)
    if found and _SAFE_CLASS.fullmatch(found.group(1)):
        return found.group(1)
    return "Failure"


def classify_skip(reason: object) -> str:
    text = sanitize_text(reason).casefold()
    if not text:
        return "unexplained"
    if any(hint in text for hint in ENVIRONMENT_SKIP_HINTS):
        return "environment"
    if any(hint in text for hint in PLATFORM_SKIP_HINTS):
        return "platform"
    return "unexplained"


def normalise_platform(value: object) -> str:
    text = str(value or "").strip().casefold()
    if text.startswith("win"):
        return "windows"
    if text.startswith(("linux", "ubuntu", "debian")):
        return "linux"
    if text.startswith(("mac", "darwin", "osx")):
        return "macos"
    return text or "unknown"


# ---------------------------------------------------------------------------
# Repository evidence
# ---------------------------------------------------------------------------

def _git(*args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def source_identity(commit: str | None = None) -> dict:
    resolved = commit or _git("rev-parse", "HEAD") or None
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or None
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    return {"commit": resolved, "branch": branch, "dirty": dirty}


def product_version() -> str | None:
    try:
        from thu_assistant.version import __version__
    except Exception:  # pragma: no cover - metadata is read without importing
        text = (ROOT / "src" / "thu_assistant" / "version.py").read_text(encoding="utf-8")
        found = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        return found.group(1) if found else None
    return __version__


def contract_inventory() -> dict:
    rows = {}
    base = ROOT / "contracts" / "v0.2"
    for path in sorted(base.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        version = value.get("x-contract-version") if isinstance(value, dict) else None
        if isinstance(version, str) and version:
            rows[path.name] = version
    return rows


def contract_set_digest(contracts: dict) -> str:
    import hashlib
    payload = json.dumps(contracts, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def runtime_environment() -> dict:
    return {
        "python": platform_module.python_version(),
        "system": platform_module.system(),
        "machine": platform_module.machine(),
    }


def node_version() -> str | None:
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True,
                                timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


# ---------------------------------------------------------------------------
# JUnit
# ---------------------------------------------------------------------------

def _testcase_file(case: ElementTree.Element) -> str:
    value = case.get("file")
    if value:
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                candidate = candidate.relative_to(ROOT)
            except ValueError:
                pass
        return candidate.as_posix()
    classname = case.get("classname") or ""
    parts = classname.split(".")
    if parts and parts[0] == "tests":
        return "/".join(parts) + ".py"
    return "tests/unknown.py"


def parse_junit(path: Path | str) -> list[dict]:
    """Read one pytest JUnit file into bounded case records."""
    document = ElementTree.parse(path)
    cases = []
    for case in document.findall(".//testcase"):
        file_name = _testcase_file(case)
        name = case.get("name") or "unknown"
        record = {
            "file": file_name,
            "name": name,
            "nodeid": f"{file_name}::{name}",
            "time": _float(case.get("time")),
            "status": "passed",
            "error_class": None,
            "skip_reason": None,
        }
        skipped = case.find("skipped")
        failure = case.find("failure")
        error = case.find("error")
        if skipped is not None:
            record["status"] = "skipped"
            record["skip_reason"] = sanitize_text(
                skipped.get("message") or skipped.text or "", limit=200)
        elif failure is not None or error is not None:
            node = failure if failure is not None else error
            record["status"] = "failed"
            declared = node.get("type") or ""
            record["error_class"] = (declared if _SAFE_CLASS.fullmatch(declared or "")
                                     else failure_class(node.get("message") or node.text))
        cases.append(record)
    return cases


def _float(value: object) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


def module_for_file(file_name: str) -> str:
    value = file_name.replace("\\", "/").lstrip("./")
    if value.startswith("tests/release_regression/") or value.startswith("tests/security_regression/"):
        return "7.2"
    if value in MODULE_BY_FILE:
        return MODULE_BY_FILE[value]
    if value.endswith(".test.js"):
        return "3.1"
    return "unmapped"


def minimal_repro(nodeid: str) -> str:
    return f'python -m pytest -o addopts= -q "{nodeid}"'


def _dev_results(path: Path | str | None) -> dict:
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _side_effects(dev: dict) -> dict:
    rows = {
        "live_campus_calls": 0,
        "model_calls": 0,
        "business_submissions": 0,
        "browser_launches": 0,
        "message_sends": 0,
        "background_processes": 0,
    }
    for key in rows:
        value = dev.get(key)
        if type(value) is int and value >= 0:
            rows[key] = value
    return rows


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(cases: list[dict], *, platform: str, commit: str | None = None,
                 dev_results: Path | str | None = None) -> dict:
    platform_name = normalise_platform(platform)
    modules: dict[str, dict] = {}
    skip_counts: dict[str, int] = {}
    skip_class: dict[str, str] = {}
    failures = []
    totals = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "total": 0}
    files = set()
    for case in cases:
        module = module_for_file(case["file"])
        row = modules.setdefault(module, {
            "id": module, "total": 0, "passed": 0, "failed": 0, "skipped": 0,
            "failures": [], "minimal_repro": [],
        })
        row["total"] += 1
        totals["total"] += 1
        files.add(case["file"])
        status = case["status"]
        if status == "passed":
            row["passed"] += 1
            totals["passed"] += 1
        elif status == "skipped":
            row["skipped"] += 1
            totals["skipped"] += 1
            reason = case.get("skip_reason") or ""
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
            skip_class.setdefault(reason, classify_skip(reason))
        else:
            row["failed"] += 1
            totals["failed"] += 1
            entry = {
                "nodeid": case["nodeid"],
                "module": module,
                "error_class": case.get("error_class") or "Failure",
                "minimal_repro": minimal_repro(case["nodeid"]),
            }
            failures.append(entry)
            row["failures"].append(entry)
            row["minimal_repro"].append(entry["minimal_repro"])
    dev = _dev_results(dev_results)
    node_fail = dev.get("node_fail")
    if type(node_fail) is int and node_fail > 0:
        totals["failed"] += node_fail
        failures.append({
            "nodeid": "tests/campus-*.test.js",
            "module": "3.1",
            "error_class": "NodeTestFailure",
            "minimal_repro": "node --test tests/campus-*.test.js",
        })
    node_passed = dev.get("node_pass")
    node_skipped = dev.get("node_skipped")
    node_totals = {
        "passed": node_passed if type(node_passed) is int else 0,
        "failed": node_fail if type(node_fail) is int else 0,
        "skipped": node_skipped if type(node_skipped) is int else 0,
    }
    totals["total"] += node_totals["passed"] + node_totals["failed"] + node_totals["skipped"]
    totals["passed"] += node_totals["passed"]
    totals["skipped"] += node_totals["skipped"]

    skip_reasons = [
        {"reason": reason, "count": count, "class": skip_class.get(reason, "unexplained")}
        for reason, count in sorted(skip_counts.items())
    ]
    gaps = []
    for row in skip_reasons:
        if row["class"] == "environment":
            gaps.append({"kind": "environment_skip", "reason": row["reason"],
                         "count": row["count"]})
        elif row["class"] == "unexplained":
            gaps.append({"kind": "unexplained_skip", "reason": row["reason"],
                         "count": row["count"]})
    side_effects = _side_effects(dev)
    if any(side_effects.values()):
        failures.append({
            "nodeid": "side-effects",
            "module": "7.2",
            "error_class": "UnexpectedSideEffect",
            "minimal_repro": "python scripts/regression_report.py report --help",
        })
    if any(row["class"] == "unexplained" for row in skip_reasons) or failures:
        status = "FAIL"
    elif gaps:
        status = "INCOMPLETE"
    elif platform_name == "unknown":
        status = "INCOMPLETE"
        gaps.append({"kind": "unknown_platform", "reason": "platform could not be identified"})
    else:
        status = "PASS"
    contracts = contract_inventory()
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": utc_now(),
        "source": source_identity(commit),
        "product_version": product_version(),
        "contracts": contracts,
        "contract_set_sha256": contract_set_digest(contracts),
        "platform": {"name": platform_name, **runtime_environment(), "node": node_version()},
        "scope": {
            "source": "pytest-junit+dev-node",
            "test_files": sorted(files),
            "modules": sorted(row["id"] for row in modules.values()),
        },
        "totals": totals,
        "node": node_totals,
        "modules": [modules[key] for key in sorted(modules)],
        "skip_reasons": skip_reasons,
        "gaps": gaps,
        "failures": failures,
        "side_effects": side_effects,
        "status": status,
    }


def write_report(report: dict, output: Path | str) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def report_summary(report: dict) -> dict:
    """A short console summary with no case-level private material."""
    return {
        "schema": report.get("schema"),
        "status": report.get("status"),
        "platform": (report.get("platform") or {}).get("name"),
        "commit": (report.get("source") or {}).get("commit"),
        "totals": report.get("totals"),
        "gaps": len(report.get("gaps") or ()),
        "failures": len(report.get("failures") or ()),
        "side_effects": report.get("side_effects"),
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def load_reports(directory: Path | str) -> list[dict]:
    rows = []
    for path in sorted(Path(directory).rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("schema") == REPORT_SCHEMA:
            rows.append(value)
    return rows


def aggregate_reports(reports: list[dict], *,
                      required_platforms=DEFAULT_REQUIRED_PLATFORMS) -> dict:
    by_platform: dict[str, list[dict]] = {}
    commits = set()
    gaps = []
    for report in reports:
        name = normalise_platform((report.get("platform") or {}).get("name"))
        by_platform.setdefault(name, []).append(report)
        commit = (report.get("source") or {}).get("commit")
        if commit:
            commits.add(commit)
        gaps.extend(report.get("gaps") or ())
    missing = [name for name in required_platforms if name not in by_platform]
    statuses = [report.get("status") for report in reports]
    if not reports or missing or len(commits) > 1 or "FAIL" in statuses:
        status = "FAIL"
    elif "INCOMPLETE" in statuses:
        status = "INCOMPLETE"
    else:
        status = "PASS"
    if missing:
        gaps.append({"kind": "platform_report_missing", "platforms": missing})
    if len(commits) > 1:
        gaps.append({"kind": "report_commit_mismatch", "commits": sorted(commits)})
    return {
        "schema": AGGREGATE_SCHEMA,
        "generated_at": utc_now(),
        "source_commit": next(iter(commits)) if len(commits) == 1 else None,
        "contracts": reports[0].get("contracts") if reports else {},
        "platforms": {
            name: [report_summary(report) for report in rows]
            for name, rows in sorted(by_platform.items())
        },
        "required_platforms": list(required_platforms),
        "missing_platforms": missing,
        "gaps": gaps,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Release gate checks
# ---------------------------------------------------------------------------

def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def privacy_scan_source() -> dict:
    scanner = _load_script("privacy_scan")
    findings = []
    count = 0
    for path in scanner.source_files():
        count += 1
        try:
            data = path.read_bytes()
        except OSError:
            findings.append({"file": path.name, "line": 0, "rule": "unreadable"})
            continue
        try:
            name = path.relative_to(ROOT).as_posix()
        except ValueError:
            name = path.name
        findings.extend(scanner.findings(name, data))
    return {"ok": not findings, "files_checked": count,
            "findings": findings[:50], "finding_count": len(findings),
            "scope": "tracked-source"}


def privacy_scan_archives(paths: list[Path]) -> dict:
    scanner = _load_script("privacy_scan")
    findings = []
    count = 0
    for path in paths:
        try:
            for name, data in scanner.archive_files(path):
                count += 1
                findings.extend(scanner.findings(path.name + "/" + name, data))
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            findings.append({"file": path.name, "line": 0,
                             "rule": "archive-unreadable:" + failure_class(type(error).__name__)})
    return {"ok": not findings, "files_checked": count,
            "findings": findings[:50], "finding_count": len(findings),
            "scope": "candidate-archives"}


def marker_findings(name: str, data: bytes) -> list[dict]:
    """Find full secret markers in public bytes without returning the value."""
    findings = []
    base = name.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if base in _CREDENTIAL_FILES or base == ".env" or (
            base.startswith(".env.") and base != ".env.example"):
        findings.append({"file": name, "line": 0, "rule": "credential-file"})
    text = data.decode("utf-8", "ignore")
    for rule, patterns in MARKER_RULES.items():
        for pattern in patterns:
            for match in pattern.finditer(text):
                findings.append({"file": name,
                                 "line": text.count("\n", 0, match.start()) + 1,
                                 "rule": rule})
                if len(findings) >= 50:
                    return findings
    return findings


def marker_scan_archives(paths: list[Path]) -> dict:
    scanner = _load_script("privacy_scan")
    findings = []
    count = 0
    for path in paths:
        try:
            for name, data in scanner.archive_files(path):
                count += 1
                findings.extend(marker_findings(path.name + "/" + name, data))
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            findings.append({"file": path.name, "line": 0,
                             "rule": "archive-unreadable:" + failure_class(type(error).__name__)})
    return {"ok": not findings, "files_checked": count,
            "findings": findings[:50], "finding_count": len(findings),
            "scope": "public-archives"}


def marker_scan_files(directory: Path | str) -> dict:
    findings = []
    count = 0
    for path in sorted(Path(directory).rglob("*")):
        if not path.is_file():
            continue
        count += 1
        try:
            findings.extend(marker_findings(path.name, path.read_bytes()))
        except OSError:
            findings.append({"file": path.name, "line": 0, "rule": "unreadable"})
    return {"ok": not findings, "files_checked": count,
            "findings": findings[:50], "finding_count": len(findings),
            "scope": "public-reports"}


def _pins(rows) -> dict[str, str]:
    values = {}
    for row in rows or ():
        text = str(row)
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$", text)
        if match:
            values[match.group(1).casefold()] = match.group(2)
    return values


def _requirements_pins(path: Path) -> dict[str, str]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                rows.append(line)
    except OSError:
        return {}
    return _pins(rows)


def dependency_audit() -> dict:
    issues = []
    try:
        spec = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {"ok": False, "issues": ["PYPROJECT_UNREADABLE"], "runtime": {}, "dev": {}}
    project = spec.get("project") or {}
    runtime = _pins(project.get("dependencies") or ())
    dev = _pins((project.get("optional-dependencies") or {}).get("dev") or ())
    if runtime != EXPECTED_RUNTIME:
        issues.append("RUNTIME_DEPENDENCY_PIN_MISMATCH")
    requirements = _requirements_pins(ROOT / "requirements.txt")
    if requirements != EXPECTED_RUNTIME:
        issues.append("REQUIREMENTS_RUNTIME_PIN_MISMATCH")
    for name in EXPECTED_RUNTIME:
        if dev.get(name):
            issues.append("RUNTIME_DEPENDENCY_IN_DEV_EXTRA:" + name)
    for name, version in EXPECTED_DEV.items():
        if dev.get(name) != version:
            issues.append("DEV_DEPENDENCY_PIN_MISMATCH:" + name)
    dev_lines = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8") \
        if (ROOT / "requirements-dev.txt").is_file() else ""
    dev_pins = _pins([line.split("#", 1)[0].strip() for line in dev_lines.splitlines()])
    for name, version in EXPECTED_DEV.items():
        if dev_pins.get(name) != version:
            issues.append("REQUIREMENTS_DEV_PIN_MISMATCH:" + name)
    node_pins = {}
    try:
        package = json.loads((ROOT / "src" / "thu_assistant" / "package.json").read_text(encoding="utf-8"))
        node_pins = {str(key).casefold(): str(value)
                     for key, value in (package.get("dependencies") or {}).items()}
    except (OSError, ValueError):
        issues.append("NODE_PACKAGE_UNREADABLE")
    if node_pins != EXPECTED_NODE:
        issues.append("NODE_DEPENDENCY_PIN_MISMATCH")
    lock_pin = None
    try:
        lock = json.loads((ROOT / "src" / "thu_assistant" / "package-lock.json").read_text(encoding="utf-8"))
        packages = lock.get("packages") or {}
        entry = packages.get("node_modules/tough-cookie") or {}
        lock_pin = entry.get("version")
        root_deps = (packages.get("") or {}).get("dependencies") or {}
        if root_deps.get("tough-cookie") != "6.0.0":
            issues.append("NODE_LOCK_ROOT_MISMATCH")
    except (OSError, ValueError):
        issues.append("NODE_LOCK_UNREADABLE")
    if lock_pin != "6.0.0":
        issues.append("NODE_LOCK_PIN_MISMATCH")
    return {"ok": not issues, "issues": sorted(set(issues)),
            "runtime": runtime, "dev": dev, "node": node_pins, "node_locked": lock_pin}


def defaults_audit(*, platform_name: str | None = None) -> dict:
    issues = []
    details = {}
    try:
        from thu_assistant import configuration
        with tempfile.TemporaryDirectory(prefix="openthu-defaults-") as tmp:
            env = {"HOME": tmp, "USERPROFILE": tmp, "LOCALAPPDATA": str(Path(tmp) / "local")}
            config = configuration.load_config(env=env, platform_name=platform_name or os.name)
            public = config.public_dict()
            details.update({
                "config_source": public.get("config_file"),
                "secrets": public.get("secrets"),
                "listen_host": public.get("network", {}).get("listen_host"),
                "page_size": public.get("ui", {}).get("page_size"),
                "refresh_seconds": public.get("ui", {}).get("refresh_seconds"),
            })
            if public.get("secrets") != "environment-only":
                issues.append("SECRETS_NOT_ENVIRONMENT_ONLY")
            if config.read_secret("MODEL_API_KEY", env={}) is not None:
                issues.append("SECRET_LEAKED_FROM_DEFAULT")
            if public.get("network", {}).get("listen_host") not in LOOPBACK_HOSTS | {"127.0.0.1"}:
                issues.append("DASHBOARD_NOT_LOOPBACK")
            if public.get("ui", {}).get("page_size") != 50:
                issues.append("PAGE_SIZE_DEFAULT_NOT_50")
    except Exception as error:
        issues.append("CONFIG_AUDIT_FAILED:" + failure_class(type(error).__name__))
    try:
        from thu_assistant.local_api import contract as local_contract
        details["local_api_bind"] = local_contract.DEFAULT_BIND
        if local_contract.DEFAULT_BIND != "127.0.0.1":
            issues.append("LOCAL_API_DEFAULT_BIND_NOT_LOOPBACK")
    except Exception as error:
        issues.append("LOCAL_API_AUDIT_FAILED:" + failure_class(type(error).__name__))
    try:
        from thu_assistant.submissions import enabled as submissions_enabled
        details["learn_submit_enabled"] = submissions_enabled()
        if submissions_enabled():
            issues.append("LEARN_SUBMIT_SWITCH_ON")
    except Exception as error:
        issues.append("SUBMISSION_AUDIT_FAILED:" + failure_class(type(error).__name__))
    return {"ok": not issues, "issues": sorted(set(issues)), "details": details}


def candidate_check(dist: Path | str) -> dict:
    directory = Path(dist)
    if not directory.is_dir():
        return {"ok": False, "state": "NOT_BUILT", "artifacts": []}
    try:
        release = _load_script("package_release")
        result = release.verify(directory)
    except Exception as error:
        return {"ok": False, "state": "VERIFY_FAILED",
                "error_class": failure_class(str(error)), "artifacts": []}
    artifacts = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix in {".whl", ".zip", ".gz"}:
            import hashlib
            artifacts.append({"name": path.name,
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"ok": bool(result.get("ok")), "state": "VERIFIED" if result.get("ok") else "INVALID",
            "artifacts": artifacts}


def _all_archives(dist: Path) -> list[Path]:
    if not dist.is_dir():
        return []
    return [path for path in sorted(dist.iterdir())
            if path.is_file() and path.suffix in {".whl", ".zip", ".gz"}]


def release_gate(*, reports_dir: Path | str | None = None, dist: Path | str = "dist",
                 require_dist: bool = False, platform_name: str | None = None) -> dict:
    checks = {}
    gaps = []
    statuses = []
    if reports_dir:
        reports = load_reports(reports_dir)
        aggregate = aggregate_reports(reports)
        checks["reports"] = {
            "ok": aggregate["status"] == "PASS",
            "status": aggregate["status"],
            "platforms": sorted(aggregate["platforms"]),
            "source_commit": aggregate["source_commit"],
            "gaps": aggregate["gaps"],
        }
        statuses.append(aggregate["status"])
        gaps.extend(aggregate["gaps"])
    privacy_source = privacy_scan_source()
    checks["privacy_source"] = privacy_source
    statuses.append("PASS" if privacy_source["ok"] else "FAIL")
    archive_paths = _all_archives(Path(dist))
    privacy_archives = privacy_scan_archives(archive_paths) if archive_paths else {
        "ok": True, "files_checked": 0, "findings": [], "finding_count": 0,
        "scope": "candidate-archives", "state": "NO_ARCHIVES",
    }
    checks["privacy_archives"] = privacy_archives
    statuses.append("PASS" if privacy_archives["ok"] else "FAIL")
    marker_archives = marker_scan_archives(archive_paths) if archive_paths else {
        "ok": True, "files_checked": 0, "findings": [], "finding_count": 0,
        "scope": "public-archives", "state": "NO_ARCHIVES",
    }
    checks["marker_archives"] = marker_archives
    statuses.append("PASS" if marker_archives["ok"] else "FAIL")
    if reports_dir:
        marker_reports = marker_scan_files(reports_dir)
        checks["marker_reports"] = marker_reports
        statuses.append("PASS" if marker_reports["ok"] else "FAIL")
    dependencies = dependency_audit()
    checks["dependencies"] = dependencies
    statuses.append("PASS" if dependencies["ok"] else "FAIL")
    defaults = defaults_audit(platform_name=platform_name)
    checks["defaults"] = defaults
    statuses.append("PASS" if defaults["ok"] else "FAIL")
    candidate = candidate_check(dist)
    checks["candidate"] = candidate
    if candidate["state"] == "NOT_BUILT":
        if require_dist:
            statuses.append("FAIL")
        else:
            statuses.append("INCOMPLETE")
            gaps.append({"kind": "candidate_not_built"})
    else:
        statuses.append("PASS" if candidate["ok"] else "FAIL")
    if "FAIL" in statuses:
        status = "FAIL"
    elif "INCOMPLETE" in statuses:
        status = "INCOMPLETE"
    else:
        status = "PASS"
    source = source_identity()
    contracts = contract_inventory()
    return {
        "schema": RELEASE_SCHEMA,
        "generated_at": utc_now(),
        "source": source,
        "product_version": product_version(),
        "contracts": contracts,
        "contract_set_sha256": contract_set_digest(contracts),
        "platform": {"name": normalise_platform(platform_name or platform_module.system())},
        "checks": checks,
        "gaps": gaps,
        "status": status,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _report_from_args(args) -> dict:
    cases = parse_junit(args.junit)
    return build_report(cases, platform=args.platform, commit=args.commit,
                        dev_results=args.dev_results)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    report = commands.add_parser("report", help="write one platform report")
    report.add_argument("--junit", required=True)
    report.add_argument("--dev-results")
    report.add_argument("--platform", default=platform_module.system())
    report.add_argument("--commit")
    report.add_argument("--output", required=True)

    gate = commands.add_parser("gate", help="write one platform report and gate it")
    gate.add_argument("--junit", required=True)
    gate.add_argument("--dev-results")
    gate.add_argument("--platform", default=platform_module.system())
    gate.add_argument("--commit")
    gate.add_argument("--output", required=True)

    aggregate = commands.add_parser("aggregate", help="combine platform reports")
    aggregate.add_argument("--reports", required=True)
    aggregate.add_argument("--output", required=True)
    aggregate.add_argument("--require-platforms", default=",".join(DEFAULT_REQUIRED_PLATFORMS))

    release = commands.add_parser("release-gate", help="privacy, dependency and candidate gate")
    release.add_argument("--reports")
    release.add_argument("--dist", default="dist")
    release.add_argument("--require-dist", action="store_true")
    release.add_argument("--output", required=True)
    release.add_argument("--platform")

    args = parser.parse_args(argv)
    if args.command in ("report", "gate"):
        report_value = _report_from_args(args)
        write_report(report_value, args.output)
        print(json.dumps(report_summary(report_value), ensure_ascii=False))
        if args.command == "report":
            return 0
        return 0 if report_value["status"] == "PASS" else 1
    if args.command == "aggregate":
        required = tuple(item.strip() for item in args.require_platforms.split(",") if item.strip())
        value = aggregate_reports(load_reports(args.reports), required_platforms=required)
        write_report(value, args.output)
        print(json.dumps({"status": value["status"],
                          "platforms": sorted(value["platforms"]),
                          "missing": value["missing_platforms"],
                          "gaps": len(value["gaps"])}, ensure_ascii=False))
        return 0 if value["status"] == "PASS" else 1
    value = release_gate(reports_dir=args.reports, dist=args.dist,
                         require_dist=args.require_dist, platform_name=args.platform)
    write_report(value, args.output)
    print(json.dumps({"status": value["status"],
                      "checks": {name: bool(row.get("ok")) for name, row in value["checks"].items()},
                      "gaps": len(value["gaps"])}, ensure_ascii=False))
    return 0 if value["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
