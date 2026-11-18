"""Version regression report, release gate and privacy aggregation (module 7.2).

The report is built from the JUnit XML pytest writes and from ``dev.py test``'s
Node summary.  It answers, in one machine-readable file, what source commit and
contracts the evidence belongs to, which platform and CI runner produced it, how
many cases ran, passed, failed or skipped, why anything was skipped, which module
a failure belongs to, and the minimal command that reproduces it.

Safety rules enforced here:

* only a failure class and a repository-relative node id leave the process; raw
  exception text, absolute paths, parameter values, tokens, cookies and account
  values never do;
* a report must carry its runtime platform and CI identity; a report whose
  declared platform differs from its host or CI runner is a shape report and
  cannot be used by the release gate;
* evidence must belong to the candidate commit, the same product version and the
  same contract set;
* there is a minimum evidence floor: empty, one-case, all-skipped, filtered and
  node-less suites are not a passing version;
* the release gate scans the source tree and every candidate archive, requires a
  readable marker set, audits the dependency pins from one manifest, checks
  conservative defaults and verifies the candidate package before it can return
  PASS.

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

REPORT_SCHEMA = "version-regression-report-v2"
AGGREGATE_SCHEMA = "version-release-report-v2"
RELEASE_SCHEMA = "version-release-gate-v2"

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "loopback"}
DEFAULT_REQUIRED_PLATFORMS = ("linux", "windows")
FLOOR_PATH = ROOT / "tests" / "release_regression" / "evidence_floor.json"
COVERAGE_MATRIX_PATH = ROOT / "tests" / "release_regression" / "coverage_matrix.json"
MARKER_DEFINITION_SUFFIX = "tests/security_regression/marker_set.json"

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
    "tests/test_batch7_integration.py": "batch-7",
    "tests/test_batch8_integration.py": "batch-8",
    "tests/test_calendar_export.py": "5.4",
    "tests/test_campus.py": "3.1",
    "tests/test_cli.py": "6.1",
    "tests/test_configuration.py": "2.1",
    "tests/test_dev_diagnostics.py": "2.1",
    "tests/test_distribution.py": "6.3",
    "tests/test_docs_consistency.py": "7.3",
    "tests/test_events.py": "5.4",
    "tests/test_facade.py": "6.1",
    "tests/test_facade_audit.py": "6.1",
    "tests/test_facade_failure_envelopes.py": "7.3",
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
_CREDENTIAL_FILES = {
    "creds.json", "credentials.json", "cookies.json", "session.json",
    "token.json", "auth.json", "hosts.yml",
}
_MARKER_LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")

# Skip reasons are an explicit registry, not substring guessing.  A new reason
# must be registered here or it fails as unexplained; platform:/env: prefixes
# are available for parameterised skips.
SKIP_CLASSES = {
    "windows acl only": "platform",
    "windows dpapi key creation": "platform",
    "windows dpapi only": "platform",
    "windows dpapi session migration": "platform",
    "windows preserves the legacy dpapi path when no new key is configured": "platform",
    "posix mode bits are not available on windows": "platform",
    "posix shell fixture": "platform",
    "non-windows production encryption failure contract": "platform",
    "posix permission bits": "platform",
    "posix permission bits are not applicable on windows": "platform",
    "posix permission bits; windows acls are covered by secure_store": "platform",
    "symlink creation needs extra privileges": "platform",
    "posix fifo; windows has no unprivileged equivalent": "platform",
    "posix mode bits; windows acls are covered by 2.2": "platform",
    "posix mode bits; ntfs acls are not mode-based": "platform",
    "descriptor accounting needs /proc": "platform",
    "posix owner/mode checks": "platform",
    "posix file-key backend; windows dpapi is covered by the windows job": "platform",
    "posix chmod semantics": "platform",
    "symbolic links unavailable on this platform": "platform",
    "this platform/user cannot create symbolic links": "platform",
    "symbolic links are not permitted in this environment": "platform",
    "symlinks are not available on this platform": "platform",
    "zip modes are not enforced for the windows build tooling": "platform",
    "macos only behaviour": "platform",
    "node.js is not installed": "environment",
    "node.js is not installed on this platform": "environment",
    "node is unavailable": "environment",
    "pillow build has no webp support on this platform": "environment",
    "pillow build cannot write animated webp": "environment",
    "this pillow build cannot write animated webp": "environment",
    "root can read any file, so the permission path cannot be exercised": "environment",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Safe text and evidence classes
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
    if text.startswith("platform:"):
        return "platform"
    if text.startswith(("environment:", "env:")):
        return "environment"
    return SKIP_CLASSES.get(text, "unexplained")


def normalise_platform(value: object) -> str:
    text = str(value or "").strip().casefold()
    if text.startswith("win"):
        return "windows"
    if text.startswith(("linux", "ubuntu", "debian")):
        return "linux"
    if text.startswith(("mac", "darwin", "osx")):
        return "macos"
    return text or "unknown"


def normalise_python(value: object) -> str | None:
    match = re.match(r"^(\d+\.\d+)", str(value or ""))
    return match.group(1) if match else None


def ci_identity(env: dict[str, str] | None = None) -> dict:
    source = os.environ if env is None else env
    return {
        "runner_os": source.get("RUNNER_OS"),
        "runner_arch": source.get("RUNNER_ARCH"),
        "run_id": source.get("GITHUB_RUN_ID"),
        "workflow": source.get("GITHUB_WORKFLOW"),
    }


def _gap(kind: str, severity: str, **details) -> dict:
    row = {"kind": kind, "severity": severity}
    for key, value in details.items():
        if value is not None:
            row[key] = value
    return row


def _dedupe_gaps(gaps: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    order: list[str] = []
    for gap in gaps:
        key = json.dumps(gap, sort_keys=True, ensure_ascii=False)
        if key not in counts:
            counts[key] = 0
            order.append(key)
        counts[key] += 1
    rows = []
    for key in order:
        row = json.loads(key)
        if counts[key] > 1:
            row["occurrences"] = counts[key]
        rows.append(row)
    return rows


def _status(gaps: list[dict], failures: list[dict], report_statuses=()) -> str:
    if "FAIL" in report_statuses or failures:
        return "FAIL"
    if any(gap.get("severity") == "fail" for gap in gaps):
        return "FAIL"
    if "INCOMPLETE" in report_statuses or gaps:
        return "INCOMPLETE"
    return "PASS"


# ---------------------------------------------------------------------------
# Floor, coverage and repository evidence
# ---------------------------------------------------------------------------

def load_floor(path: Path | str = FLOOR_PATH) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def covered_test_files(path: Path | str = COVERAGE_MATRIX_PATH) -> tuple[set[str], str | None]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), "COVERAGE_MATRIX_UNREADABLE"
    modules = value.get("modules") if isinstance(value, dict) else None
    if not isinstance(modules, list):
        return set(), "COVERAGE_MATRIX_INVALID"
    files = set()
    for module in modules:
        if isinstance(module, dict) and module.get("state") == "covered":
            for name in module.get("test_files") or ():
                if isinstance(name, str) and name.startswith("tests/"):
                    files.add(name)
    return files, None


def _git(*args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def source_identity(commit: str | None = None) -> dict:
    resolved = commit or _git("rev-parse", "HEAD") or os.environ.get("GITHUB_SHA") or None
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or os.environ.get("GITHUB_REF_NAME") or None
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

def _safe_file(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "tests/unknown.py"
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(ROOT)
        except ValueError:
            return "tests/unknown.py"
    normal = candidate.as_posix()
    if normal.startswith("./"):
        normal = normal[2:]
    parts = Path(normal).parts
    if not normal.startswith("tests/") or ".." in parts:
        return "tests/unknown.py"
    cleaned = sanitize_text(normal, limit=240)
    return cleaned if cleaned.startswith("tests/") else "tests/unknown.py"


def _testcase_file(case: ElementTree.Element) -> str:
    value = case.get("file")
    if value:
        return _safe_file(value)
    classname = sanitize_text(case.get("classname") or "", limit=240)
    parts = classname.split(".")
    if parts and parts[0] == "tests":
        return _safe_file("/".join(parts) + ".py")
    return "tests/unknown.py"


def parse_junit(path: Path | str) -> list[dict]:
    """Read one pytest JUnit file into bounded, sanitized case records."""
    document = ElementTree.parse(path)
    cases = []
    for case in document.findall(".//testcase"):
        file_name = _testcase_file(case)
        name = sanitize_text(case.get("name") or "unknown", limit=240) or "unknown"
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
        elif error is not None:
            record["status"] = "error"
            declared = error.get("type") or ""
            record["error_class"] = (declared if _SAFE_CLASS.fullmatch(declared or "")
                                     else failure_class(error.get("message") or error.text))
        elif failure is not None:
            record["status"] = "failed"
            declared = failure.get("type") or ""
            record["error_class"] = (declared if _SAFE_CLASS.fullmatch(declared or "")
                                     else failure_class(failure.get("message") or failure.text))
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


def _dev_results(path: Path | str | None) -> tuple[dict, bool]:
    if not path:
        return {}, False
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, False
    return (value, True) if isinstance(value, dict) else ({}, False)


def _side_effects(dev: dict, present: bool) -> tuple[dict, bool]:
    rows = {"live_campus_calls": 0, "model_calls": 0, "business_submissions": 0}
    invalid = False
    for key in rows:
        if key not in dev:
            continue
        value = dev[key]
        if type(value) is not int or value < 0:
            invalid = True
            continue
        rows[key] = value
    return rows, invalid


def _node_totals(dev: dict, present: bool) -> dict:
    rows = {"passed": 0, "failed": 0, "skipped": 0}
    keys = {"passed": "node_pass", "failed": "node_fail", "skipped": "node_skipped"}
    for key, source in keys.items():
        value = dev.get(source) if present else None
        if type(value) is int and value >= 0:
            rows[key] = value
    return rows


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(cases: list[dict], *, platform: str, commit: str | None = None,
                 dev_results: Path | str | None = None,
                 enforce_floor: bool = True, floor: dict | None = None,
                 ci: dict | None = None) -> dict:
    platform_name = normalise_platform(platform)
    floor_value = floor if floor is not None else load_floor()
    dev, dev_present = _dev_results(dev_results)
    modules: dict[str, dict] = {}
    skip_counts: dict[str, int] = {}
    skip_class: dict[str, str] = {}
    failures = []
    totals = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "total": 0}
    files = set()
    gaps: list[dict] = []

    def module_row(name: str) -> dict:
        return modules.setdefault(name, {
            "id": name, "total": 0, "passed": 0, "failed": 0, "skipped": 0,
            "errors": 0, "failures": [], "minimal_repro": [],
        })

    for case in cases:
        module = module_for_file(case["file"])
        row = module_row(module)
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
            if status == "error":
                row["errors"] += 1
                totals["errors"] += 1
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

    node_totals = _node_totals(dev, dev_present)
    node_total = sum(node_totals.values())
    if node_total:
        row = module_row("3.1")
        row["node"] = node_totals
        row["total"] += node_total
        row["passed"] += node_totals["passed"]
        row["failed"] += node_totals["failed"]
        row["skipped"] += node_totals["skipped"]
        totals["total"] += node_total
        totals["passed"] += node_totals["passed"]
        totals["failed"] += node_totals["failed"]
        totals["skipped"] += node_totals["skipped"]
    if node_totals["failed"]:
        entry = {
            "nodeid": "tests/campus-*.test.js",
            "module": "3.1",
            "error_class": "NodeTestFailure",
            "minimal_repro": "node --test tests/campus-*.test.js",
        }
        failures.append(entry)
        module_row("3.1")["failures"].append(entry)
        module_row("3.1")["minimal_repro"].append(entry["minimal_repro"])

    skip_reasons = [
        {"reason": reason, "count": count, "class": skip_class.get(reason, "unexplained")}
        for reason, count in sorted(skip_counts.items())
    ]
    for row in skip_reasons:
        if row["class"] == "environment":
            gaps.append(_gap("environment_skip", "incomplete", reason=row["reason"],
                             count=row["count"]))
        elif row["class"] == "unexplained":
            gaps.append(_gap("unexplained_skip", "fail", reason=row["reason"],
                             count=row["count"]))
    side_effects, invalid_side_effects = _side_effects(dev, dev_present)
    if invalid_side_effects:
        gaps.append(_gap("side_effect_value_invalid", "fail"))
    if any(side_effects.values()):
        failures.append({
            "nodeid": "side-effects",
            "module": "7.2",
            "error_class": "UnexpectedSideEffect",
            "minimal_repro": "python scripts/regression_report.py report --help",
        })

    if enforce_floor:
        _apply_floor(gaps, floor_value, totals, node_totals, dev_present, files)
    if platform_name == "unknown":
        gaps.append(_gap("unknown_platform", "fail", reason="platform could not be identified"))
    if any(entry["module"] == "unmapped" for entry in failures):
        gaps.append(_gap("unmapped_failure", "fail"))

    status = _status(gaps, failures)
    contracts = contract_inventory()
    platform_block = {"name": platform_name, **runtime_environment(), "node": node_version(),
                      "ci": ci if ci is not None else ci_identity()}
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": utc_now(),
        "source": source_identity(commit),
        "product_version": product_version(),
        "contracts": contracts,
        "contract_set_sha256": contract_set_digest(contracts),
        "platform": platform_block,
        "scope": {
            "source": "pytest-junit+dev-node",
            "test_files": sorted(files),
            "modules": sorted(row["id"] for row in modules.values()),
        },
        "evidence_floor": {
            "enforced": bool(enforce_floor),
            "min_total": floor_value.get("min_total") if isinstance(floor_value, dict) else None,
            "max_skip_ratio": floor_value.get("max_skip_ratio") if isinstance(floor_value, dict) else None,
        },
        "totals": totals,
        "node": node_totals,
        "modules": [modules[key] for key in sorted(modules)],
        "skip_reasons": skip_reasons,
        "gaps": _dedupe_gaps(gaps),
        "failures": failures,
        "side_effects": side_effects,
        "side_effect_source": "dev-results" if dev_present else "absent",
        "status": status,
    }


def _apply_floor(gaps: list[dict], floor: dict, totals: dict, node_totals: dict,
                 dev_present: bool, files: set[str]) -> None:
    if not floor:
        gaps.append(_gap("evidence_floor_missing", "fail"))
        return
    min_total = floor.get("min_total")
    if type(min_total) is not int or min_total < 1:
        gaps.append(_gap("evidence_floor_invalid", "fail", field="min_total"))
    elif totals["total"] == 0:
        gaps.append(_gap("evidence_empty", "fail"))
    elif totals["passed"] < min_total:
        gaps.append(_gap("evidence_below_floor", "fail", passed=totals["passed"],
                         min_total=min_total))
    max_skip = floor.get("max_skip_ratio")
    if isinstance(max_skip, (int, float)) and totals["total"]:
        ratio = totals["skipped"] / totals["total"]
        if ratio > float(max_skip):
            gaps.append(_gap("skip_ratio_exceeded", "incomplete",
                             skipped=totals["skipped"], total=totals["total"],
                             ratio=round(ratio, 4), max_ratio=float(max_skip)))
    if floor.get("require_node", True) and sum(node_totals.values()) == 0:
        gaps.append(_gap("node_evidence_missing", "incomplete"))
    if floor.get("require_dev_results", True) and not dev_present:
        gaps.append(_gap("side_effect_evidence_missing", "incomplete"))
    expected, error = covered_test_files()
    if error:
        gaps.append(_gap(error.lower(), "fail"))
    else:
        missing = sorted(expected - files)
        if missing:
            gaps.append(_gap("test_file_coverage_incomplete", "fail",
                             missing_count=len(missing), missing=missing[:10]))


def write_report(report: dict, output: Path | str) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def report_summary(report: dict) -> dict:
    """A short console summary with no case-level private material."""
    platform_block = report.get("platform") or {}
    return {
        "schema": report.get("schema"),
        "status": report.get("status"),
        "platform": platform_block.get("name"),
        "system": platform_block.get("system"),
        "ci_runner_os": (platform_block.get("ci") or {}).get("runner_os"),
        "commit": (report.get("source") or {}).get("commit"),
        "totals": report.get("totals"),
        "gaps": len(report.get("gaps") or ()),
        "failures": len(report.get("failures") or ()),
        "side_effect_source": report.get("side_effect_source"),
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def load_reports_detail(directory: Path | str) -> tuple[list[dict], list[dict]]:
    rows = []
    errors = []
    for path in sorted(Path(directory).rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            errors.append({"file": path.name, "rule": "unreadable"})
            continue
        if not isinstance(value, dict) or value.get("schema") != REPORT_SCHEMA:
            errors.append({"file": path.name, "rule": "schema_mismatch"})
            continue
        rows.append(value)
    return rows, errors


def load_reports(directory: Path | str) -> list[dict]:
    return load_reports_detail(directory)[0]


def _platform_identity(report: dict) -> tuple[str, str, str | None]:
    block = report.get("platform") or {}
    name = normalise_platform(block.get("name"))
    host = normalise_platform(block.get("system"))
    ci = block.get("ci") if isinstance(block.get("ci"), dict) else {}
    runner = normalise_platform(ci.get("runner_os")) if ci.get("runner_os") else None
    return name, host, runner


def aggregate_reports(reports: list[dict], *,
                      required_platforms=DEFAULT_REQUIRED_PLATFORMS,
                      report_errors: list[dict] | None = None,
                      allow_shape_reports: bool = False,
                      expected_commit: str | None = None,
                      expected_product_version: str | None = None,
                      expected_contracts_digest: str | None = None,
                      required_python: dict[str, list[str]] | None = None) -> dict:
    by_platform: dict[str, list[dict]] = {}
    commits = set()
    products = set()
    digests = set()
    gaps: list[dict] = []
    statuses = []
    for report in reports:
        name, host, runner = _platform_identity(report)
        by_platform.setdefault(name, []).append(report)
        statuses.append(report.get("status"))
        source = report.get("source") or {}
        commit = source.get("commit")
        if not commit:
            gaps.append(_gap("report_commit_missing", "fail", report=name))
        else:
            commits.add(commit)
        if source.get("dirty") is True:
            gaps.append(_gap("report_worktree_dirty", "fail", report=name))
        if not allow_shape_reports:
            if host != name:
                gaps.append(_gap("platform_identity_mismatch", "fail",
                                 declared=name, host=host))
            if runner and runner != name:
                gaps.append(_gap("ci_platform_identity_mismatch", "fail",
                                 declared=name, ci_runner_os=runner))
            if not runner:
                gaps.append(_gap("platform_identity_unverified", "incomplete",
                                 platform=name))
        product = report.get("product_version")
        if product:
            products.add(product)
        else:
            gaps.append(_gap("report_product_version_missing", "fail", report=name))
        digest = report.get("contract_set_sha256")
        if digest:
            digests.add(digest)
        else:
            gaps.append(_gap("report_contracts_missing", "fail", report=name))
        gaps.extend(report.get("gaps") or ())
    missing = [name for name in required_platforms if name not in by_platform]
    if not reports:
        gaps.append(_gap("test_evidence_missing", "fail"))
    if missing:
        gaps.append(_gap("platform_report_missing", "fail", platforms=missing))
    if len(commits) > 1:
        gaps.append(_gap("report_commit_mismatch", "fail", commits=sorted(commits)))
    if expected_commit and commits and commits != {expected_commit}:
        gaps.append(_gap("report_commit_mismatch", "fail", expected_commit=expected_commit,
                         commits=sorted(commits)))
    if len(products) > 1:
        gaps.append(_gap("report_product_mismatch", "fail", products=sorted(products)))
    if expected_product_version and products and products != {expected_product_version}:
        gaps.append(_gap("report_product_mismatch", "fail",
                         expected=expected_product_version, products=sorted(products)))
    if len(digests) > 1:
        gaps.append(_gap("report_contracts_mismatch", "fail", digests=sorted(digests)))
    if expected_contracts_digest and digests and digests != {expected_contracts_digest}:
        gaps.append(_gap("report_contracts_mismatch", "fail",
                         expected=expected_contracts_digest, digests=sorted(digests)))
    if required_python:
        for platform_name, versions in required_python.items():
            found = {normalise_python((row.get("platform") or {}).get("python"))
                     for row in by_platform.get(platform_name, [])}
            absent = [version for version in versions if version not in found]
            if absent:
                gaps.append(_gap("python_matrix_incomplete", "fail",
                                 platform=platform_name, missing=absent))
    for error in report_errors or ():
        gaps.append(_gap("report_unreadable", "fail", **error))
    gaps = _dedupe_gaps(gaps)
    status = _status(gaps, [], statuses)
    source_commit = next(iter(commits)) if len(commits) == 1 else None
    return {
        "schema": AGGREGATE_SCHEMA,
        "generated_at": utc_now(),
        "source_commit": source_commit,
        "product_version": next(iter(products)) if len(products) == 1 else None,
        "contracts": reports[0].get("contracts") if reports else {},
        "contract_set_sha256": next(iter(digests)) if len(digests) == 1 else None,
        "platforms": {
            name: [report_summary(report) for report in rows]
            for name, rows in sorted(by_platform.items())
        },
        "required_platforms": list(required_platforms),
        "required_python": required_python or {},
        "missing_platforms": missing,
        "report_errors": list(report_errors or ()),
        "allow_shape_reports": bool(allow_shape_reports),
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
            "scope": "worktree-source"}


def privacy_scan_archives(paths: list[Path]) -> dict:
    scanner = _load_script("privacy_scan")
    findings = []
    count = 0
    archives = []
    for path in paths:
        members = 0
        local = []
        try:
            for name, data in scanner.archive_files(path):
                members += 1
                count += 1
                local.extend(scanner.findings(path.name + "/" + name, data))
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            local.append({"file": path.name, "line": 0,
                          "rule": "archive-unreadable:" + failure_class(type(error).__name__)})
        findings.extend(local)
        archives.append({"name": path.name, "members": members,
                         "finding_count": len(local), "ok": not local})
    return {"ok": not findings, "files_checked": count,
            "findings": findings[:50], "finding_count": len(findings),
            "archives": archives, "scope": "candidate-archives"}


def _skip_marker_definition(member: str) -> bool:
    return member.replace("\\", "/").endswith(MARKER_DEFINITION_SUFFIX)


def marker_findings(name: str, data: bytes,
                    markers: dict[str, str] | None = None) -> list[dict]:
    """Find exact injected marker values in public bytes.

    Generic token/path regexes are deliberately not used here: product source
    contains regex literals and synthetic examples that are not leaks.  The
    caller supplies the marker set it actually injected; only those exact values
    (or a credential-shaped file name) fail the gate.
    """
    findings = []
    base = name.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if base in _CREDENTIAL_FILES or base == ".env" or (
            base.startswith(".env.") and base != ".env.example"):
        findings.append({"file": name, "line": 0, "rule": "credential-file"})
    for label, value in (markers or {}).items():
        if not isinstance(value, str) or not value:
            continue
        rule_label = label if _MARKER_LABEL.fullmatch(label) else "value"
        needle = value.encode("utf-8")
        start = data.find(needle)
        if start < 0:
            continue
        findings.append({"file": name, "line": data.count(b"\n", 0, start) + 1,
                         "rule": "marker:" + rule_label})
    return findings


def marker_scan_archives(paths: list[Path], markers: dict[str, str] | None = None) -> dict:
    if not markers:
        return {"ok": False, "files_checked": 0, "findings": [], "finding_count": 0,
                "marker_count": 0, "archives": [], "state": "NO_MARKERS",
                "scope": "public-archives"}
    scanner = _load_script("privacy_scan")
    findings = []
    count = 0
    archives = []
    for path in paths:
        members = 0
        local = []
        try:
            for name, data in scanner.archive_files(path):
                if _skip_marker_definition(name):
                    continue
                members += 1
                count += 1
                local.extend(marker_findings(path.name + "/" + name, data, markers))
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            local.append({"file": path.name, "line": 0,
                          "rule": "archive-unreadable:" + failure_class(type(error).__name__)})
        findings.extend(local)
        archives.append({"name": path.name, "members": members,
                         "finding_count": len(local), "ok": not local})
    return {"ok": not findings, "files_checked": count,
            "findings": findings[:50], "finding_count": len(findings),
            "marker_count": len(markers or {}), "archives": archives,
            "scope": "public-archives"}


def marker_scan_files(directory: Path | str, markers: dict[str, str] | None = None) -> dict:
    findings = []
    count = 0
    for path in sorted(Path(directory).rglob("*")):
        if not path.is_file():
            continue
        count += 1
        try:
            findings.extend(marker_findings(path.name, path.read_bytes(), markers))
        except OSError:
            findings.append({"file": path.name, "line": 0, "rule": "unreadable"})
    return {"ok": not findings, "files_checked": count,
            "findings": findings[:50], "finding_count": len(findings),
            "marker_count": len(markers or {}), "scope": "public-reports"}


def _spec_map(rows) -> dict[str, str]:
    values = {}
    for row in rows or ():
        text = str(row).strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*(==|>=)\s*([^\s;]+)$", text)
        if not match:
            continue
        name = match.group(1).casefold()
        operator = match.group(2)
        version = match.group(3)
        values[name] = (">=" + version) if operator == ">=" else version
    return values


def _manifest_specs(mapping) -> dict[str, str]:
    values = {}
    for name, value in (mapping or {}).items():
        text = str(value).strip()
        values[str(name).casefold()] = text[2:] if text.startswith("==") else text
    return values


def _requirements_specs(path: Path) -> dict[str, str]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                rows.append(line)
    except OSError:
        return {}
    return _spec_map(rows)


def dependency_audit(root: Path | str = ROOT) -> dict:
    base = Path(root)
    issues = []
    try:
        distribution = json.loads((base / "build_support" / "distribution.json")
                                  .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "source": "build_support/distribution.json",
                "issues": ["DISTRIBUTION_MANIFEST_UNREADABLE"]}
    dependencies = distribution.get("dependencies") if isinstance(distribution, dict) else {}
    if not isinstance(dependencies, dict):
        return {"ok": False, "source": "build_support/distribution.json",
                "issues": ["DISTRIBUTION_DEPENDENCIES_MISSING"]}
    runtime = _manifest_specs(dependencies.get("runtime"))
    dev = _manifest_specs(dependencies.get("development"))
    node = _manifest_specs(dependencies.get("node"))
    optional = dependencies.get("optional") if isinstance(dependencies.get("optional"), dict) else {}
    browser = _manifest_specs((optional.get("browser") or {})) if isinstance(optional, dict) else {}
    try:
        spec = tomllib.loads((base / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {"ok": False, "source": "build_support/distribution.json",
                "issues": ["PYPROJECT_UNREADABLE"]}
    project = spec.get("project") or {}
    py_runtime = _spec_map(project.get("dependencies") or ())
    py_dev = _spec_map((project.get("optional-dependencies") or {}).get("dev") or ())
    py_browser = _spec_map((project.get("optional-dependencies") or {}).get("browser") or ())
    if py_runtime != runtime:
        issues.append("RUNTIME_DEPENDENCY_PIN_MISMATCH")
    if _requirements_specs(base / "requirements.txt") != runtime:
        issues.append("REQUIREMENTS_RUNTIME_PIN_MISMATCH")
    # setuptools is a build requirement in pyproject and a development pin in
    # the distribution manifest; compare it separately instead of duplicating it
    # in the optional dev extra.
    py_dev_compare = {name: value for name, value in py_dev.items() if name != "setuptools"}
    declared_dev_compare = {name: value for name, value in dev.items() if name != "setuptools"}
    if py_dev_compare != declared_dev_compare:
        issues.append("DEV_DEPENDENCY_PIN_MISMATCH")
    build_requires = _spec_map((spec.get("build-system") or {}).get("requires") or ())
    if dev.get("setuptools") and build_requires.get("setuptools") != dev["setuptools"]:
        issues.append("BUILD_DEPENDENCY_PIN_MISMATCH")
    if _requirements_specs(base / "requirements-dev.txt") != dev:
        issues.append("REQUIREMENTS_DEV_PIN_MISMATCH")
    if py_browser != browser:
        issues.append("BROWSER_DEPENDENCY_PIN_MISMATCH")
    for name in runtime:
        if name in dev:
            issues.append("RUNTIME_DEPENDENCY_IN_DEV_EXTRA:" + name)
    node_pins = {}
    try:
        package = json.loads((base / "src" / "thu_assistant" / "package.json")
                             .read_text(encoding="utf-8"))
        node_pins = _manifest_specs(package.get("dependencies"))
    except (OSError, ValueError):
        issues.append("NODE_PACKAGE_UNREADABLE")
    if node_pins != node:
        issues.append("NODE_DEPENDENCY_PIN_MISMATCH")
    lock_pin = None
    try:
        lock = json.loads((base / "src" / "thu_assistant" / "package-lock.json")
                          .read_text(encoding="utf-8"))
        packages = lock.get("packages") or {}
        entry = packages.get("node_modules/tough-cookie") or {}
        lock_pin = entry.get("version")
        root_deps = _manifest_specs((packages.get("") or {}).get("dependencies"))
        if root_deps != node:
            issues.append("NODE_LOCK_ROOT_MISMATCH")
    except (OSError, ValueError):
        issues.append("NODE_LOCK_UNREADABLE")
    if node and lock_pin != node.get("tough-cookie"):
        issues.append("NODE_LOCK_PIN_MISMATCH")
    return {"ok": not issues, "source": "build_support/distribution.json",
            "issues": sorted(set(issues)), "runtime": runtime, "dev": dev,
            "node": node, "node_locked": lock_pin}


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
            if public.get("network", {}).get("listen_host") not in LOOPBACK_HOSTS:
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


def _artifact_names(directory: Path) -> list[str]:
    return [path.name for path in sorted(directory.iterdir())
            if path.is_file() and path.suffix in {".whl", ".zip", ".gz"}]


def candidate_check(dist: Path | str) -> dict:
    directory = Path(dist)
    if not directory.is_dir():
        return {"ok": False, "state": "NOT_BUILT", "artifacts": []}
    try:
        release = _load_script("package_release")
        result = release.verify(directory)
    except Exception as error:
        code = getattr(error, "code", None)
        error_class = code if isinstance(code, str) and _SAFE_CLASS.fullmatch(code) else failure_class(str(error))
        return {"ok": False, "state": "VERIFY_FAILED",
                "error_class": error_class, "artifacts": _artifact_names(directory)}
    artifacts = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix in {".whl", ".zip", ".gz"}:
            import hashlib
            artifacts.append({"name": path.name,
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    ok = bool(result.get("ok"))
    return {"ok": ok, "state": "VERIFIED" if ok else "INVALID",
            "error_class": None if ok else "CANDIDATE_INVALID",
            "artifacts": artifacts}


def _all_archives(dist: Path) -> list[Path]:
    if not dist.is_dir():
        return []
    return [path for path in sorted(dist.iterdir())
            if path.is_file() and path.suffix in {".whl", ".zip", ".gz"}]


def release_gate(*, reports_dir: Path | str | None = None, dist: Path | str = "dist",
                 require_dist: bool = False, platform_name: str | None = None,
                 markers: dict[str, str] | None = None,
                 marker_error: str | None = None,
                 require_markers: bool | None = None,
                 expected_commit: str | None = None,
                 require_python_matrix: bool = True) -> dict:
    checks = {}
    gaps: list[dict] = []
    statuses: list[str] = []
    current = source_identity()
    floor = load_floor()
    if reports_dir:
        reports, report_errors = load_reports_detail(reports_dir)
        aggregate = aggregate_reports(
            reports, report_errors=report_errors,
            expected_commit=expected_commit or current.get("commit"),
            expected_product_version=product_version(),
            expected_contracts_digest=contract_set_digest(contract_inventory()),
            required_python=floor.get("required_python") if require_python_matrix else None)
        checks["reports"] = {
            "ok": aggregate["status"] == "PASS",
            "status": aggregate["status"],
            "platforms": sorted(aggregate["platforms"]),
            "source_commit": aggregate["source_commit"],
            "gaps": aggregate["gaps"],
            "report_errors": aggregate["report_errors"],
        }
        statuses.append(aggregate["status"])
        gaps.extend(aggregate["gaps"])
    else:
        checks["reports"] = {
            "ok": False, "status": "MISSING", "platforms": [],
            "source_commit": None,
            "gaps": [_gap("test_evidence_missing", "fail")],
        }
        statuses.append("FAIL")
        gaps.append(_gap("test_evidence_missing", "fail"))

    marker_required = require_markers if require_markers is not None else require_dist
    marker_ok = not marker_error and bool(markers)
    marker_state = "OK" if marker_ok else (marker_error or "MARKER_SET_EMPTY")
    checks["marker_set"] = {"ok": marker_ok, "state": marker_state,
                            "count": len(markers or {})}
    if not marker_ok:
        statuses.append("FAIL" if marker_required else "INCOMPLETE")
        gaps.append(_gap("marker_set_unavailable", "fail" if marker_required else "incomplete",
                         state=marker_state))

    privacy_source = privacy_scan_source()
    checks["privacy_source"] = privacy_source
    statuses.append("PASS" if privacy_source["ok"] else "FAIL")
    archive_paths = _all_archives(Path(dist))
    privacy_archives = privacy_scan_archives(archive_paths) if archive_paths else {
        "ok": True, "files_checked": 0, "findings": [], "finding_count": 0,
        "archives": [], "scope": "candidate-archives", "state": "NO_ARCHIVES",
    }
    checks["privacy_archives"] = privacy_archives
    statuses.append("PASS" if privacy_archives["ok"] else "FAIL")
    marker_archives = marker_scan_archives(archive_paths, markers) if archive_paths else {
        "ok": False, "files_checked": 0, "findings": [], "finding_count": 0,
        "marker_count": len(markers or {}), "archives": [],
        "scope": "public-archives", "state": "NO_ARCHIVES",
    }
    if not marker_ok:
        marker_archives["ok"] = False
        marker_archives["state"] = marker_state
    checks["marker_archives"] = marker_archives
    statuses.append("PASS" if marker_archives["ok"] else
                    ("FAIL" if marker_required else "INCOMPLETE"))
    if reports_dir:
        marker_reports = marker_scan_files(reports_dir, markers)
        checks["marker_reports"] = marker_reports
        statuses.append("PASS" if marker_reports["ok"] else
                        ("FAIL" if marker_required else "INCOMPLETE"))
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
            gaps.append(_gap("candidate_not_built", "fail"))
        else:
            statuses.append("INCOMPLETE")
            gaps.append(_gap("candidate_not_built", "incomplete"))
    else:
        statuses.append("PASS" if candidate["ok"] else "FAIL")
    gaps = _dedupe_gaps(gaps)
    status = _status(gaps, [], statuses)
    contracts = contract_inventory()
    return {
        "schema": RELEASE_SCHEMA,
        "generated_at": utc_now(),
        "source": current,
        "expected_commit": expected_commit or current.get("commit"),
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

def load_markers(path: Path | str | None, *, with_error: bool = False):
    """Read an exact marker set; with_error also returns a stable error code."""
    if not path:
        result = ({}, "MARKER_SET_REQUIRED")
        return result if with_error else result[0]
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        result = ({}, "MARKER_SET_MISSING")
        return result if with_error else result[0]
    except (OSError, ValueError):
        result = ({}, "MARKER_SET_UNREADABLE")
        return result if with_error else result[0]
    if not isinstance(value, dict):
        result = ({}, "MARKER_SET_INVALID")
        return result if with_error else result[0]
    markers = {str(key): str(item) for key, item in value.items()
               if isinstance(item, str) and item and not str(key).startswith("_")}
    if not markers:
        result = ({}, "MARKER_SET_EMPTY")
        return result if with_error else result[0]
    result = (markers, None)
    return result if with_error else result[0]


def _report_from_args(args) -> dict:
    cases = parse_junit(args.junit)
    return build_report(cases, platform=args.platform, commit=args.commit,
                        dev_results=args.dev_results,
                        enforce_floor=not getattr(args, "diagnostic", False))


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
    report.add_argument("--diagnostic", action="store_true",
                        help="skip the evidence floor for a local probe")

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
    aggregate.add_argument("--expected-commit")
    aggregate.add_argument("--allow-shape-reports", action="store_true",
                           help="diagnostic only; never use for release evidence")

    release = commands.add_parser("release-gate", help="privacy, dependency and candidate gate")
    release.add_argument("--reports")
    release.add_argument("--dist", default="dist")
    release.add_argument("--require-dist", action="store_true")
    release.add_argument("--output", required=True)
    release.add_argument("--platform")
    release.add_argument("--markers", help="JSON object of exact injected marker values")
    release.add_argument("--expected-commit")
    release.add_argument("--require-python-matrix", action=argparse.BooleanOptionalAction,
                         default=True,
                         help="require the evidence-floor Python matrix (default; "
                              "use --no-require-python-matrix only for diagnostics)")

    args = parser.parse_args(argv)
    if args.command in ("report", "gate"):
        report_value = _report_from_args(args)
        write_report(report_value, args.output)
        print(json.dumps(report_summary(report_value), ensure_ascii=False))
        if args.command == "report" and getattr(args, "diagnostic", False):
            return 0
        return 0 if report_value["status"] == "PASS" else 1
    if args.command == "aggregate":
        required = tuple(item.strip() for item in args.require_platforms.split(",") if item.strip())
        reports, errors = load_reports_detail(args.reports)
        value = aggregate_reports(reports, required_platforms=required,
                                  report_errors=errors,
                                  allow_shape_reports=args.allow_shape_reports,
                                  expected_commit=args.expected_commit)
        write_report(value, args.output)
        print(json.dumps({"status": value["status"],
                          "platforms": sorted(value["platforms"]),
                          "missing": value["missing_platforms"],
                          "gaps": len(value["gaps"])}, ensure_ascii=False))
        return 0 if value["status"] == "PASS" else 1
    markers, marker_error = load_markers(args.markers, with_error=True)
    value = release_gate(reports_dir=args.reports, dist=args.dist,
                         require_dist=args.require_dist, platform_name=args.platform,
                         markers=markers, marker_error=marker_error,
                         expected_commit=args.expected_commit,
                         require_python_matrix=args.require_python_matrix)
    write_report(value, args.output)
    print(json.dumps({"status": value["status"],
                      "checks": {name: bool(row.get("ok")) for name, row in value["checks"].items()},
                      "gaps": len(value["gaps"])}, ensure_ascii=False))
    return 0 if value["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
