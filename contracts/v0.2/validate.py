#!/usr/bin/env python3
"""Validate the frozen v0.2 plan, contracts, ownership, gates and acceptance map.

This is an offline structural check. It does not prove real campus behavior or
replace the module tests; it catches missing contracts, unresolvable module
dependencies, same-batch path conflicts and accidental private paths.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts" / "v0.2"
PLAN = ROOT / "plans" / "v0.2.0"
DELIVERY = PLAN / "交付"

EXPECTED_CONTRACTS = {
    "result-envelope.schema.json": "result-envelope-v1",
    "error-classification.json": "error-classification-v1",
    "storage-header.schema.json": "secure-storage-header-v1",
    "source-evidence.schema.json": "source-evidence-v1",
    "media-reference.schema.json": "media-reference-v1",
    "calendar-event.schema.json": "calendar-event-v1",
    "business-facade.schema.json": "business-facade-v1",
    "local-read-api.schema.json": "local-read-api-v1",
}
EXTRA_CONTRACTS = {"compatibility-baseline-v1"}
REQUIRED_CONTRACT_KEYS = (
    "x-contract-version",
    "x-status",
    "x-semantics",
    "x-unknown-values",
    "x-compatibility-sample",
    "x-rejection-samples",
)
PRIVATE_PATTERNS = (
    re.compile("/" + r"home/[^/\s\"']+"),
    re.compile(r"C:\\\\Users\\\\", re.I),
    re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    re.compile(r"password\s*=", re.I),
    re.compile(r"api[_-]?key\s*=", re.I),
)

errors: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"cannot read {path.relative_to(ROOT)}: {type(exc).__name__}")
        return None


def contract_ids() -> set[str]:
    return set(EXPECTED_CONTRACTS.values()) | EXTRA_CONTRACTS


def validate_contracts() -> None:
    files = sorted(p.name for p in CONTRACTS.glob("*.json"))
    expected = sorted(list(EXPECTED_CONTRACTS) + ["compatibility-baseline.json"])
    check(files == expected,
          "contract JSON file set does not match the frozen set")
    loaded = {}
    for name, version in EXPECTED_CONTRACTS.items():
        data = load(CONTRACTS / name)
        if not isinstance(data, dict):
            continue
        loaded[name] = data
        for key in REQUIRED_CONTRACT_KEYS:
            check(key in data, f"{name}: missing {key}")
        check(data.get("x-contract-version") == version,
              f"{name}: expected contract version {version}")
        check(data.get("x-status") == "frozen", f"{name}: contract is not frozen")
        check(isinstance(data.get("x-unknown-values"), dict),
              f"{name}: unknown-value policy must be an object")
        sample = data.get("x-compatibility-sample")
        check(isinstance(sample, dict) and bool(sample),
              f"{name}: missing compatibility sample")
        rejected = data.get("x-rejection-samples")
        check(isinstance(rejected, list) and len(rejected) >= 2,
              f"{name}: needs at least two rejection samples")
        if isinstance(rejected, list):
            for index, item in enumerate(rejected):
                check(isinstance(item, dict) and item.get("why") and "value" in item,
                      f"{name}: rejection sample {index} needs why and value")
        if isinstance(sample, dict):
            for field in data.get("required", []):
                check(field in sample, f"{name}: compatibility sample misses {field}")

    result = loaded.get("result-envelope.schema.json", {})
    for field in ("implementation_state", "runtime_availability", "evidence_level"):
        check(field in result.get("properties", {}),
              f"result envelope: missing optional {field}")

    errors_contract = loaded.get("error-classification.json", {})
    categories = set(errors_contract.get("categories", {}))
    codes = errors_contract.get("codes", {})
    check(categories == {"INPUT", "AUTH", "PERMISSION", "RATE_LIMIT", "MAINTENANCE", "NETWORK",
                         "UPSTREAM_CONTRACT", "COMPLETENESS", "CONFLICT", "NOT_IMPLEMENTED",
                         "CANCELLED", "INTERNAL"}, "error categories changed")
    check(isinstance(codes, dict) and codes, "error code registry is empty")
    check(all(value in categories for value in codes.values()),
          "an error code points at an unknown category")
    check(errors_contract.get("write_replay_policy", {}).get("after_dispatch", "").startswith("Never replay"),
          "write replay policy must forbid automatic replay after dispatch")

    storage = loaded.get("storage-header.schema.json", {})
    check(storage.get("x-cipher-suite") == "AES-256-GCM",
          "storage cipher suite must remain authenticated AES-256-GCM")
    purposes = storage.get("properties", {}).get("purpose", {}).get("enum", [])
    check(set(purposes) == {"CREDENTIAL", "SESSION", "ACTION", "INDEX", "MEDIA", "EVENT_CURSOR"},
          "storage purposes changed")

    media = loaded.get("media-reference.schema.json", {})
    limits = media.get("x-image-limits", {})
    check(limits.get("max_bytes_per_image") == 8388608, "image byte limit changed")
    check(limits.get("max_images_per_request") == 4, "image count limit changed")
    check(limits.get("max_total_bytes_per_request") == 16777216, "total image limit changed")
    check(limits.get("max_pixels_per_image") == 16000000, "image pixel limit changed")

    facade = loaded.get("business-facade.schema.json", {})
    check(set(facade.get("methods", {})) == {"query", "health", "prepare", "execute", "result", "cancel"},
          "business facade method set changed")
    check(facade.get("x-legacy-tool-count") == 29, "facade legacy tool count must stay 29")
    check(facade.get("methods", {}).get("execute", {}).get("requires_out_of_band_host_authorization") is True,
          "execute must require trusted out-of-band host authorization")
    check(facade.get("methods", {}).get("cancel", {}).get("side_effect") == "LOCAL_REQUEST_CANCELLATION_ONLY",
          "cancel must remain local request cancellation only")

    local = loaded.get("local-read-api.schema.json", {})
    check(local.get("x-read-only") is True, "local API must be read-only")
    check(local.get("x-write-endpoints") == [], "local API cannot expose write endpoints")
    check(local.get("bind", {}).get("public_bind") is False, "local API cannot bind publicly by default")
    check(set(local.get("security", {}).get("method_allowlist", [])) == {"GET", "HEAD"},
          "local API methods must remain GET/HEAD")
    check(local.get("limits", {}).get("request_body_bytes") == 65536, "local API body limit changed")
    check(local.get("limits", {}).get("page_size_default") == 50, "local API page default changed")
    check(local.get("limits", {}).get("page_size_max") == 200, "local API page maximum changed")

    compat = load(CONTRACTS / "compatibility-baseline.json")
    if isinstance(compat, dict):
        tools = compat.get("mcp_tools", [])
        check(len(tools) == 29 and len(set(tools)) == 29, "compatibility baseline must have 29 unique tools")
        check(len(compat.get("entry_points", [])) == 6, "compatibility baseline must have 6 entry points")
        check(set(compat.get("legacy_result_fields", [])) >=
              {"status", "source", "checked_at", "complete", "source_complete", "warnings", "verification_level"},
              "compatibility baseline misses a legacy result field")
        check(compat.get("mcp_tool_count") == 29, "compatibility tool count must be 29")


def validate_delivery() -> None:
    manifest = load(PLAN / "02-字数与依赖校验.json")
    if not isinstance(manifest, dict):
        return
    plan_ids = [row["id"] for row in manifest.get("modules", [])]
    plan_by_id = {row["id"]: row for row in manifest.get("modules", [])}
    check(len(plan_ids) == 24 and len(set(plan_ids)) == 24, "plan must contain 24 unique modules")

    ownership = load(DELIVERY / "模块负责表.json")
    if isinstance(ownership, dict):
        modules = ownership.get("modules", [])
        ids = [m.get("id") for m in modules]
        check(ids == plan_ids, "ownership module order/ids do not match the plan")
        check(len(modules) == 24, "ownership must cover 24 modules")
        orders = [m.get("integration_order") for m in modules]
        check(orders == list(range(1, 25)), "integration order must be 1..24")
        batch_paths: dict[int, dict[str, str]] = {}
        for module in modules:
            mid = module.get("id")
            base = plan_by_id.get(mid)
            if not base:
                continue
            check(module.get("batch") == base.get("batch"), f"{mid}: batch differs from plan")
            check(set(module.get("dependencies", [])) == set(base.get("dependencies", [])),
                  f"{mid}: dependencies differ from plan")
            check(set(module.get("parallel_peers", [])) == set(base.get("parallel_peers", [])),
                  f"{mid}: parallel peers differ from plan")
            check(bool(module.get("owner")), f"{mid}: missing owner")
            check(bool(module.get("exclusive_paths")), f"{mid}: missing exclusive paths")
            check(bool(module.get("input_contracts")), f"{mid}: missing input contracts")
            check(bool(module.get("outputs")), f"{mid}: missing outputs")
            for contract in module.get("input_contracts", []):
                check(contract in contract_ids(), f"{mid}: unknown input contract {contract}")
            for path in module.get("exclusive_paths", []):
                batch = module.get("batch")
                previous = batch_paths.setdefault(batch, {})
                check(path not in previous,
                      f"same-batch path conflict in batch {batch}: {path} owned by {previous.get(path)} and {mid}")
                previous[path] = mid
        if any(module.get("batch") == 1 and module.get("status") == "accepted" for module in modules):
            check(True, "batch 1 ownership accepted")

    gates = load(DELIVERY / "批次门禁.json")
    if isinstance(gates, dict):
        batches = gates.get("batches", [])
        check(len(batches) == 9, "batch gates must contain 9 batches")
        covered = [mid for batch in batches for mid in batch.get("modules", [])]
        check(sorted(covered) == sorted(plan_ids), "batch gates do not cover all 24 modules")
        by_batch = {batch["batch"]: batch for batch in batches}
        check(by_batch.get(1, {}).get("status") == "accepted", "batch 1 must be accepted")
        check(by_batch.get(2, {}).get("status") == "ready", "batch 2 must be ready after batch 1")
        for number in range(3, 10):
            check(by_batch.get(number, {}).get("status") == "locked",
                  f"batch {number} must remain locked before its predecessor is accepted")
        for number in range(2, 10):
            expected = set(by_batch[number - 1].get("modules", []))
            check(all(dep in expected for dep in plan_by_id[[m for m in plan_ids if plan_by_id[m]["batch"] == number][0]].get("dependencies", [])),
                  f"batch {number} dependencies do not point only at batch {number - 1}")

    acceptance = load(DELIVERY / "验收映射.json")
    if isinstance(acceptance, dict):
        rows = acceptance.get("modules", [])
        check([row.get("id") for row in rows] == plan_ids, "acceptance map ids/order do not match the plan")
        for row in rows:
            check(bool(row.get("contracts")), f"{row.get('id')}: acceptance map misses contracts")
            check(bool(row.get("tests")), f"{row.get('id')}: acceptance map misses tests")
            check(bool(row.get("docs")), f"{row.get('id')}: acceptance map misses docs")
            check(bool(row.get("install_assets")), f"{row.get('id')}: acceptance map misses install assets")
            if row.get("id") != "1.1":
                ownership_row = next((m for m in ownership.get("modules", []) if m.get("id") == row.get("id")), {}) if isinstance(ownership, dict) else {}
                has_code = any(path.startswith("src/") or path.endswith((".py", ".js", ".toml")) for path in ownership_row.get("exclusive_paths", []))
                if has_code:
                    check(bool(row.get("code_paths")), f"{row.get('id')}: acceptance map misses code paths")

    version = load(PLAN / "VERSION_MANIFEST.json")
    if isinstance(version, dict):
        check(version.get("target_version") == "0.2.0", "version manifest target is not 0.2.0")
        check(version.get("package_version_at_baseline") == "0.1.0",
              "1.1 must not switch the package version")
        check(version.get("release_status") == "candidate-not-released",
              "version must remain candidate-not-released")
        check(version.get("modules", {}).get("total") == 24, "version manifest module total changed")
        check(version.get("batches", {}).get("total") == 9, "version manifest batch total changed")
        check(re.fullmatch(r"[a-f0-9]{40}", str(version.get("baseline", {}).get("integration_merge_commit", ""))),
              "version manifest integration commit is not a full hash")


def validate_plan_copy() -> None:
    validator = PLAN / "工具" / "validate_plan.py"
    check(validator.is_file(), "copied plan validator is missing")
    if validator.is_file():
        result = subprocess.run([sys.executable, str(validator)], capture_output=True, text=True, check=False)
        check(result.returncode == 0, "copied plan validator failed")
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            errors.append("copied plan validator did not return JSON")
        else:
            check(payload.get("ok") is True, "copied plan validator reported errors")
            check(payload.get("modules") == 24 and payload.get("batches") == 9,
                  "copied plan validator reported wrong module/batch count")


def validate_privacy() -> None:
    for base in (CONTRACTS, PLAN):
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".json", ".md", ".py", ".txt"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                errors.append(f"cannot read {path.relative_to(ROOT)} for privacy scan")
                continue
            for pattern in PRIVATE_PATTERNS:
                if pattern.search(text):
                    errors.append(f"private-looking value in {path.relative_to(ROOT)}: {pattern.pattern}")


validate_contracts()
validate_delivery()
validate_plan_copy()
validate_privacy()

print(json.dumps({
    "ok": not errors,
    "contracts": len(EXPECTED_CONTRACTS) + 1,
    "modules": 24,
    "batches": 9,
    "legacy_tools": 29,
    "errors": errors,
}, ensure_ascii=False))
sys.exit(0 if not errors else 1)
