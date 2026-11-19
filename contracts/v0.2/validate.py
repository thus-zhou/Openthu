#!/usr/bin/env python3
"""Validate the frozen v0.2 plan, contracts, samples, ownership and gates.

The validator combines metadata checks, JSON Schema Draft 2020-12 instance
validation and explicit semantic checks for contracts that are registries or
facade documents rather than plain JSON Schemas. It is offline and never
contacts a campus service.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts" / "v0.2"
SAMPLES = CONTRACTS / "samples"
PLAN = ROOT / "plans" / "v0.2.0"
DELIVERY = PLAN / "交付"

errors: list[str] = []

try:
    from jsonschema import Draft202012Validator, FormatChecker
    from jsonschema.exceptions import SchemaError
except ImportError:
    Draft202012Validator = None
    FormatChecker = None
    SchemaError = Exception
    errors.append("jsonschema is required; install requirements-dev.txt")

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
ALL_CONTRACTS = {**EXPECTED_CONTRACTS, "compatibility-baseline.json": "compatibility-baseline-v1"}
REQUIRED_CONTRACT_KEYS = (
    "x-contract-version",
    "x-owner",
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
CORE_LEGACY_FIELDS = {
    "status", "source", "checked_at", "complete", "source_complete",
    "warnings", "verification_level",
}


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
    return set(ALL_CONTRACTS.values())


def schema_validator(schema: dict):
    if Draft202012Validator is None:
        return None
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        errors.append(f"invalid JSON Schema: {exc.message}")
        return None
    return Draft202012Validator(schema, format_checker=FormatChecker())


def first_error(validator, instance) -> str:
    if validator is None:
        return "validator unavailable"
    found = sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
    if not found:
        return "no error"
    return found[0].message


def calendar_semantically_invalid(value: dict) -> bool:
    time = value.get("time")
    if not isinstance(time, dict) or time.get("kind") != "DATE_TIME":
        return False
    start, end = time.get("start"), time.get("end")
    if not start or not end:
        return False
    try:
        return datetime.fromisoformat(end) < datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return False


def semantic_invalid(contract: str, value: dict) -> bool:
    if contract == "calendar-event.schema.json":
        return calendar_semantically_invalid(value)
    return False


def error_sample_valid(data: dict, sample: dict) -> bool:
    code = sample.get("code")
    category = data.get("codes", {}).get(code)
    if not category:
        return False
    if sample.get("category") != category:
        return False
    policy = data.get("categories", {}).get(category, {})
    if sample.get("retryable") is not policy.get("retryable"):
        return False
    if sample.get("automatic_replay_allowed") is not False:
        return False
    if sample.get("user_action") != policy.get("user_action"):
        return False
    return True


def business_sample_valid(data: dict, sample: dict) -> bool:
    method = sample.get("method")
    spec = data.get("methods", {}).get(method)
    request = sample.get("request")
    if not isinstance(spec, dict) or not isinstance(request, dict):
        return False
    for field in spec.get("request_required", []):
        if field not in request:
            return False
    if method == "query" and set(request) & {"method", "headers", "token", "authorization"}:
        return False
    if method == "execute":
        host = sample.get("host_authorization")
        if not isinstance(host, dict):
            return False
        for field in spec.get("host_authorization_required", []):
            if field not in host:
                return False
    if method == "cancel" and set(request) & {"service_id", "operation"}:
        return False
    if method == "prepare" and request.get("operation") not in data.get("x-registered-action-operations", []):
        return False
    return True


def local_sample_valid(data: dict, sample: dict) -> bool:
    bind = sample.get("bind")
    if bind is not None and bind not in data.get("bind", {}).get("allowed", []):
        return False
    method = sample.get("method")
    if method is not None and method not in data.get("security", {}).get("method_allowlist", []):
        return False
    path = sample.get("path")
    if path:
        prefixes = ("/api/v1/health", "/api/v1/services", "/api/v1/query/",
                    "/api/v1/todos", "/api/v1/calendar", "/api/v1/resources")
        if not any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in prefixes):
            return False
    headers = sample.get("headers")
    if isinstance(headers, dict) and headers.get("Access-Control-Allow-Origin") == "*":
        return False
    query = sample.get("query")
    if isinstance(query, dict):
        page_size = query.get("page_size")
        if page_size is not None:
            if not isinstance(page_size, int) or isinstance(page_size, bool):
                return False
            if page_size < 1 or page_size > data.get("limits", {}).get("page_size_max", 0):
                return False
        if set(query) & {"token", "authorization", "cookie"}:
            return False
    return True


def compatibility_sample_valid(data: dict, sample: dict) -> bool:
    if sample.get("tool") not in data.get("mcp_tools", []):
        return False
    fields = set(sample.get("legacy_response_fields", []))
    if not CORE_LEGACY_FIELDS <= fields:
        return False
    if "rename" in sample or "remove_field" in sample:
        return False
    if sample.get("add_to") == "query_service" and sample.get("field") == "method":
        return False
    return True


def validate_contracts(loaded: dict) -> dict:
    counts = {"schema_samples": 0, "rejection_samples": 0, "extra_samples": 0}
    files = sorted(p.name for p in CONTRACTS.glob("*.json"))
    expected = sorted(ALL_CONTRACTS)
    check(files == expected, "contract JSON file set does not match the frozen set")

    for name, version in ALL_CONTRACTS.items():
        data = loaded.get(name)
        if not isinstance(data, dict):
            continue
        for key in REQUIRED_CONTRACT_KEYS:
            check(key in data, f"{name}: missing {key}")
        check(data.get("x-contract-version") == version,
              f"{name}: expected contract version {version}")
        check(data.get("x-status") == "frozen", f"{name}: contract is not frozen")
        check(data.get("x-owner") == "1.1/Hermes", f"{name}: wrong contract owner")
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

        if "type" in data and "properties" in data:
            validator = schema_validator(data)
            if validator is not None and isinstance(sample, dict):
                counts["schema_samples"] += 1
                check(validator.is_valid(sample),
                      f"{name}: compatibility sample is not schema-valid: {first_error(validator, sample)}")
            if validator is not None and isinstance(rejected, list):
                for index, item in enumerate(rejected):
                    value = item.get("value") if isinstance(item, dict) else None
                    if not isinstance(value, dict):
                        continue
                    counts["rejection_samples"] += 1
                    if validator.is_valid(value) and not semantic_invalid(name, value):
                        errors.append(f"{name}: rejection sample {index} was accepted")
        else:
            predicate = {
                "error-classification.json": error_sample_valid,
                "business-facade.schema.json": business_sample_valid,
                "local-read-api.schema.json": local_sample_valid,
                "compatibility-baseline.json": compatibility_sample_valid,
            }.get(name)
            if predicate and isinstance(sample, dict):
                counts["schema_samples"] += 1
                check(predicate(data, sample), f"{name}: compatibility sample failed semantic validation")
            if predicate and isinstance(rejected, list):
                for index, item in enumerate(rejected):
                    value = item.get("value") if isinstance(item, dict) else None
                    if not isinstance(value, dict):
                        continue
                    counts["rejection_samples"] += 1
                    if predicate(data, value):
                        errors.append(f"{name}: rejection sample {index} was accepted by semantic validation")

    result = loaded.get("result-envelope.schema.json", {})
    for field in ("implementation_state", "runtime_availability", "evidence_level"):
        check(field in result.get("properties", {}),
              f"result envelope: missing optional {field}")
    check("legacy_error" in result.get("$defs", {}),
          "result envelope: missing legacy_error shape")

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

    compat = loaded.get("compatibility-baseline.json", {})
    if isinstance(compat, dict):
        tools = compat.get("mcp_tools", [])
        check(len(tools) == 29 and len(set(tools)) == 29, "compatibility baseline must have 29 unique tools")
        check(len(compat.get("entry_points", [])) == 6, "compatibility baseline must have 6 entry points")
        check(CORE_LEGACY_FIELDS <= set(compat.get("legacy_result_fields", [])),
              "compatibility baseline misses a legacy result field")
        check({"data", "error", "next_cursor", "missing_pages"} <=
              set(compat.get("legacy_result_fields", [])),
              "compatibility baseline misses data/error/next_cursor/missing_pages")
        check(compat.get("mcp_tool_count") == 29, "compatibility tool count must be 29")
        check(isinstance(compat.get("legacy_error_sample"), dict),
              "compatibility baseline misses the legacy error sample")
        defaults = compat.get("legacy_field_defaults", {})
        check(defaults.get("missing_pages") == [] and defaults.get("next_cursor") is None,
              "legacy field defaults are not frozen")

    return counts


def validate_extra_samples(loaded: dict) -> int:
    count = 0
    result_schema = loaded.get("result-envelope.schema.json")
    source_schema = loaded.get("source-evidence.schema.json")
    if not isinstance(result_schema, dict) or not isinstance(source_schema, dict):
        return count
    result_validator = schema_validator(result_schema)
    source_validator = schema_validator(source_schema)
    if result_validator is None or source_validator is None:
        return count

    states = load(SAMPLES / "source-states.json")
    if isinstance(states, dict):
        by_name = {}
        for item in states.get("samples", []):
            name = item.get("name")
            evidence = item.get("evidence")
            if not isinstance(evidence, dict):
                errors.append(f"source-states: {name} has no evidence object")
                continue
            count += 1
            by_name[name] = evidence
            check(source_validator.is_valid(evidence),
                  f"source-states/{name}: not schema-valid: {first_error(source_validator, evidence)}")
        failure = by_name.get("source_failure", {})
        empty = by_name.get("real_empty", {})
        partial = by_name.get("partial", {})
        check(failure.get("state") not in ("EMPTY", "OK") and failure.get("complete") is False,
              "source-states: failure must not be empty or complete")
        check(failure.get("upstream_total") is None,
              "source-states: failure must keep upstream_total unknown")
        check(empty.get("state") == "EMPTY" and empty.get("complete") is True and
              empty.get("source_complete") is True and empty.get("rows") == [],
              "source-states: real empty fixture is inconsistent")
        check(partial.get("state") == "PARTIAL" and partial.get("complete") is False and
              partial.get("source_complete") is False and bool(partial.get("missing_pages")),
              "source-states: partial fixture is inconsistent")

    blocked = load(SAMPLES / "blocked-source.json")
    if isinstance(blocked, dict) and isinstance(blocked.get("envelope"), dict):
        count += 1
        envelope = blocked["envelope"]
        check(result_validator.is_valid(envelope),
              f"blocked-source: not schema-valid: {first_error(result_validator, envelope)}")
        check(envelope.get("implementation_state") == "BLOCKED",
              "blocked-source: implementation must be BLOCKED")
        check(envelope.get("runtime_availability") == "UNAVAILABLE",
              "blocked-source: runtime must be UNAVAILABLE")
        check(envelope.get("evidence_level") == "NONE",
              "blocked-source: evidence must be NONE")
        check(envelope.get("complete") is False,
              "blocked-source: blocked source cannot be complete")
        check(envelope.get("error", {}).get("category") == "NOT_IMPLEMENTED",
              "blocked-source: error category must be NOT_IMPLEMENTED")

    stability = load(SAMPLES / "stability.json")
    if isinstance(stability, dict):
        for pair in stability.get("pairs", []):
            items = pair.get("items", [])
            count += 1
            check(len(items) == 2, f"stability/{pair.get('kind')}: expected two items")
            if len(items) == 2:
                check(items[0].get("title") == items[1].get("title"),
                      f"stability/{pair.get('kind')}: titles must match for this fixture")
                check(items[0].get("stable_resource_uid") != items[1].get("stable_resource_uid"),
                      f"stability/{pair.get('kind')}: stable identifiers must differ")
                check(items[0].get("source_record_id") != items[1].get("source_record_id"),
                      f"stability/{pair.get('kind')}: source identifiers must differ")

    compat = loaded.get("compatibility-baseline.json")
    if isinstance(compat, dict) and isinstance(compat.get("legacy_error_sample"), dict):
        count += 1
        legacy = compat["legacy_error_sample"]
        check(result_validator.is_valid(legacy),
              f"legacy error sample is not schema-valid: {first_error(result_validator, legacy)}")
        check(set(legacy.get("error", {})) <= {"code", "message"},
              "legacy error sample must keep the old code/message-only shape")
        check(legacy.get("data") is None and legacy.get("complete") is False,
              "legacy error sample cannot imply success")

    return count


def batch_state_errors(batches: list[dict], serial_gate_batches=()) -> list[str]:
    """Validate a monotonic batch state sequence without hard-coding batch numbers.

    The legal sequence is: zero or more accepted batches, exactly one ready
    batch at the first unfinished position, then locked batches. All accepted
    is legal only at the end of the version. The serial-gate batches keep their
    single-module, serial mode; they do not get a special status exception.
    """
    problems: list[str] = []
    ordered = sorted(batches, key=lambda item: item.get("batch", 0))
    numbers = [item.get("batch") for item in ordered]
    if numbers != list(range(1, len(ordered) + 1)):
        problems.append("batch numbers must be contiguous starting at 1")
    statuses = {item.get("batch"): item.get("status") for item in ordered}
    for number, status in statuses.items():
        if status not in {"accepted", "ready", "locked"}:
            problems.append(f"batch {number}: invalid status {status!r}")
    ready = [number for number in numbers if statuses.get(number) == "ready"]
    if len(ready) > 1:
        problems.append(f"exactly one batch may be ready; found {ready}")
    first_unaccepted = next((number for number in numbers if statuses.get(number) != "accepted"), None)
    if first_unaccepted is not None:
        if statuses.get(first_unaccepted) != "ready":
            problems.append(f"batch {first_unaccepted} is the first unfinished batch and must be ready")
        for number in numbers:
            if number > first_unaccepted and statuses.get(number) != "locked":
                problems.append(f"batch {number} must be locked after ready batch {first_unaccepted}")
        if ready and ready[0] != first_unaccepted:
            problems.append(f"ready batch must be {first_unaccepted}, found {ready[0]}")
    seen_ready = False
    for number in numbers:
        status = statuses.get(number)
        if status == "ready":
            seen_ready = True
        elif seen_ready and status == "accepted":
            problems.append(f"batch {number} is accepted after a ready batch")
    by_batch = {item.get("batch"): item for item in ordered}
    for number in serial_gate_batches:
        gate = by_batch.get(number)
        if gate is None:
            problems.append(f"serial gate batch {number} is missing")
            continue
        if gate.get("mode") != "serial_gate":
            problems.append(f"batch {number} is a serial gate but mode is {gate.get('mode')!r}")
        if len(gate.get("modules", [])) != 1:
            problems.append(f"batch {number} is a serial gate and must contain exactly one module")
    return problems


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

    gates = load(DELIVERY / "批次门禁.json")
    if isinstance(gates, dict):
        batches = gates.get("batches", [])
        check(len(batches) == 9, "batch gates must contain 9 batches")
        covered = [mid for batch in batches for mid in batch.get("modules", [])]
        check(sorted(covered) == sorted(plan_ids), "batch gates do not cover all 24 modules")
        by_batch = {batch["batch"]: batch for batch in batches}
        serial_gate_batches = set(gates.get("rules", {}).get("serial_gate_batches", []))
        errors.extend(batch_state_errors(batches, serial_gate_batches))
        for number in range(2, 10):
            expected = set(by_batch[number - 1].get("modules", []))
            first_id = [m for m in plan_ids if plan_by_id[m]["batch"] == number][0]
            dependencies = set(plan_by_id[first_id].get("dependencies", []))
            check(dependencies <= expected, f"batch {number} dependencies do not point only at batch {number - 1}")

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


def main() -> int:
    loaded = {name: load(CONTRACTS / name) for name in ALL_CONTRACTS}
    loaded = {name: data for name, data in loaded.items() if isinstance(data, dict)}
    counts = validate_contracts(loaded) if len(loaded) == len(ALL_CONTRACTS) else {"schema_samples": 0, "rejection_samples": 0, "extra_samples": 0}
    counts["extra_samples"] = validate_extra_samples(loaded) if len(loaded) == len(ALL_CONTRACTS) else 0
    validate_delivery()
    validate_plan_copy()
    validate_privacy()
    print(json.dumps({
        "ok": not errors,
        "contracts": len(EXPECTED_CONTRACTS),
        "compatibility_baselines": 1,
        "schema_samples": counts["schema_samples"],
        "rejection_samples": counts["rejection_samples"],
        "extra_samples": counts["extra_samples"],
        "modules": 24,
        "batches": 9,
        "legacy_tools": 29,
        "errors": errors,
    }, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
