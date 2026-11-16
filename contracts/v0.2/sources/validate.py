#!/usr/bin/env python3
"""Validate the v0.2 source register, canonical models, readiness and fixtures.

The validator is offline. It never contacts a campus service and never reads a
user's credentials, cookies or business records.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator, FormatChecker
    from jsonschema.exceptions import SchemaError
except ImportError:  # pragma: no cover - exercised only when deps are missing
    Draft202012Validator = None
    FormatChecker = None
    SchemaError = Exception

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts" / "v0.2"
SOURCES = CONTRACTS / "sources"
MODELS = SOURCES / "data-models"
FIXTURES = ROOT / "tests" / "fixtures" / "sources"
DOCS = ROOT / "docs" / "contracts"

errors: list[str] = []

START_CONCLUSIONS = {
    "CONTRACT_READY",
    "CONTRACT_READY_WITH_LIMITS",
    "BLOCKED_SOURCE_EVIDENCE",
    "BLOCKED_ACCOUNT_PERMISSION",
    "NOT_IMPLEMENTED",
}
TASK_STATUSES = {"OPEN", "BLOCKED", "LIMITED", "READY_FOR_SOURCE_REVIEW"}
PRIVATE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9.-])/" + "home/" + r"[^/\s\"']+/"),
    re.compile(r"C:\\\\Users\\\\", re.I),
    re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    re.compile(r"password\s*=", re.I),
    re.compile(r"api[_-]?key\s*=", re.I),
    re.compile(r"\"(?:cookie|authorization|access_token|refresh_token|secret)\"\s*:", re.I),
)


def check(condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"cannot read {path.relative_to(ROOT)}: {type(exc).__name__}")
        return None


def validator(schema: dict):
    if Draft202012Validator is None:
        errors.append("jsonschema is required; install requirements-dev.txt")
        return None
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        errors.append(f"invalid JSON Schema {schema.get('$id', '?')}: {exc.message}")
        return None
    return Draft202012Validator(schema, format_checker=FormatChecker())


def first_error(instance_validator, instance) -> str:
    if instance_validator is None:
        return "validator unavailable"
    found = sorted(instance_validator.iter_errors(instance), key=lambda item: list(item.path))
    return "no error" if not found else found[0].message


def _resolve_property(schema: dict, name: str):
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if isinstance(properties, dict) and name in properties:
        return properties[name]
    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                found = _resolve_property(branch, name)
                if found is not None:
                    return found
    return None


def resolve_canonical_path(schema: dict, path: str) -> bool:
    current = schema
    for part in path.split("."):
        if not isinstance(current, dict):
            return False
        if current.get("type") == "array":
            current = current.get("items", {})
        found = _resolve_property(current, part)
        if found is None:
            return False
        current = found
    return True


def renewal_semantic_errors(record) -> list[str]:
    if not isinstance(record, dict):
        return ["renewal record must be an object"]
    errors: list[str] = []
    loan_uid = record.get("loan_uid")
    renewal_uid = record.get("renewal_uid")
    if isinstance(loan_uid, str) and isinstance(renewal_uid, str):
        if not renewal_uid.endswith(":RENEWAL:" + loan_uid):
            errors.append("renewal_uid must end with ':RENEWAL:<loan_uid>'")
    state = record.get("execution_state")
    if state in {"FAILED", "REJECTED", "NOT_ATTEMPTED"}:
        before = record.get("due_date_before")
        after = record.get("due_date_after")
        if after is not None and after != before:
            errors.append("failed or unexecuted renewal must not change due_date_after")
        if record.get("verified") is True:
            errors.append("failed or unexecuted renewal cannot be verified")
    if state == "ACCEPTED":
        if record.get("verified") is not True:
            errors.append("accepted renewal must be verified")
        if not record.get("due_date_after"):
            errors.append("accepted renewal must have a new due_date_after")
    return errors


def submission_semantic_errors(record) -> list[str]:
    if not isinstance(record, dict):
        return ["submission record must be an object"]
    errors: list[str] = []
    state = record.get("state")
    if state == "SUBMITTED":
        if record.get("is_final") is not True:
            errors.append("SUBMITTED must be final")
        if record.get("result_observed") is not True:
            errors.append("SUBMITTED requires result_observed=true")
        if not record.get("remote_submission_id"):
            errors.append("SUBMITTED requires a remote_submission_id")
    if state == "UPLOADED_STAGING" and not record.get("upload_receipt_id"):
        errors.append("UPLOADED_STAGING requires an upload_receipt_id")
    return errors


def model_image_semantic_errors(record) -> list[str]:
    if not isinstance(record, dict):
        return ["model capability record must be an object"]
    errors: list[str] = []
    if record.get("capability_state") == "AVAILABLE":
        if record.get("evidence_level") not in {"SYNTHETIC_TRANSPORT", "LIVE_READ"}:
            errors.append("AVAILABLE requires transport evidence, not only an offline contract")
    return errors


def semantic_errors_for(schema_path: Path, record) -> list[str]:
    if schema_path.name == "renewal.schema.json":
        return renewal_semantic_errors(record)
    if schema_path.name == "submission.schema.json":
        return submission_semantic_errors(record)
    if schema_path.name == "model-image-capability.schema.json":
        return model_image_semantic_errors(record)
    return []


def validate_schema_contract(path: Path, expected_owner: str = "2.3/Hermes",
                             semantic=None) -> tuple[int, int]:
    data = load(path)
    if not isinstance(data, dict):
        return 0, 0
    for key in (
        "x-contract-version",
        "x-owner",
        "x-status",
        "x-semantics",
        "x-unknown-values",
        "x-compatibility-sample",
        "x-rejection-samples",
    ):
        check(key in data, f"{path.relative_to(ROOT)}: missing {key}")
    check(data.get("x-owner") == expected_owner, f"{path.relative_to(ROOT)}: wrong owner")
    check(data.get("x-status") == "frozen", f"{path.relative_to(ROOT)}: not frozen")
    schema_validator = validator(data)
    samples = 0
    rejections = 0
    sample = data.get("x-compatibility-sample")
    if schema_validator is not None and isinstance(sample, dict):
        samples += 1
        semantic_problems = semantic(sample) if semantic is not None else []
        check(
            schema_validator.is_valid(sample) and not semantic_problems,
            f"{path.relative_to(ROOT)}: compatibility sample is invalid: "
            f"{first_error(schema_validator, sample) if not schema_validator.is_valid(sample) else '; '.join(semantic_problems)}",
        )
    rejected = data.get("x-rejection-samples")
    if schema_validator is not None and isinstance(rejected, list):
        for index, item in enumerate(rejected):
            value = item.get("value") if isinstance(item, dict) else None
            if not isinstance(value, dict):
                errors.append(f"{path.relative_to(ROOT)}: rejection sample {index} is not an object")
                continue
            rejections += 1
            semantic_problems = semantic(value) if semantic is not None else []
            check(
                not schema_validator.is_valid(value) or bool(semantic_problems),
                f"{path.relative_to(ROOT)}: rejection sample {index} was accepted",
            )
    return samples, rejections


def registry_source_invalid(row: dict) -> bool:
    if not isinstance(row, dict):
        return True
    if row.get("raw_payload_included") is not False:
        return True
    planned = row.get("access") == "PLANNED_UNVERIFIED" or row.get("source_kind") == "PLANNED_UNVERIFIED"
    if planned:
        if row.get("implementation_state") not in ("PLANNED", "BLOCKED"):
            return True
        if row.get("runtime_availability") != "UNAVAILABLE":
            return True
        if row.get("evidence_level") != "NONE":
            return True
    if row.get("source_kind") == "PLANNED_UNVERIFIED" and row.get("adapter_version") != "NONE":
        return True
    if row.get("write_side_effects") == "CAMPUS_WRITE" and not row.get("unresolved"):
        return True
    if row.get("implementation_state") == "BLOCKED" and not row.get("unresolved"):
        return True
    return False


def canonical_schema_map() -> dict[str, dict]:
    paths = {
        "event": CONTRACTS / "calendar-event.schema.json",
        "course": MODELS / "course.schema.json",
        "resource": MODELS / "resource.schema.json",
        "grade": MODELS / "grade.schema.json",
        "order": MODELS / "order.schema.json",
        "attachment": MODELS / "attachment.schema.json",
        "assignment": MODELS / "assignment.schema.json",
        "submission": MODELS / "submission.schema.json",
        "renewal": MODELS / "renewal.schema.json",
        "model_image": MODELS / "model-image-capability.schema.json",
    }
    result = {}
    for name, path in paths.items():
        data = load(path)
        if isinstance(data, dict):
            result[name] = data
    return result


def validate_mapping_targets(sources: list) -> None:
    schemas = canonical_schema_map()
    for row in sources:
        if not isinstance(row, dict):
            continue
        sid = row.get("source_id")
        for mapping in row.get("field_mappings", []):
            canonical = mapping.get("canonical_field") if isinstance(mapping, dict) else None
            if not canonical:
                continue
            model, separator, canonical_path = canonical.partition(".")
            if not separator or not canonical_path:
                errors.append(f"{sid}: canonical field {canonical} has no model property")
                continue
            schema = schemas.get(model)
            if not isinstance(schema, dict):
                errors.append(f"{sid}: unknown canonical model {model}")
                continue
            if not resolve_canonical_path(schema, canonical_path):
                errors.append(f"{sid}: canonical field {canonical} does not exist in {model} schema")


def validate_code_refs(sources: list) -> None:
    for row in sources:
        if not isinstance(row, dict):
            continue
        sid = row.get("source_id")
        refs = row.get("code_refs")
        check(isinstance(refs, list), f"{sid}: code_refs must be an array")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, str):
                errors.append(f"{sid}: code reference is not a string")
                continue
            match = re.fullmatch(r"([^:]+)(?::(\d+)(?:-(\d+))?)?", ref)
            if not match:
                errors.append(f"{sid}: invalid code reference {ref}")
                continue
            path = ROOT / match.group(1)
            if not path.is_file():
                errors.append(f"{sid}: missing code reference file {match.group(1)}")
                continue
            if match.group(2):
                line_count = len(path.read_text(encoding="utf-8").splitlines())
                start = int(match.group(2))
                end = int(match.group(3) or start)
                if start < 1 or end < start or end > line_count:
                    errors.append(f"{sid}: code reference range out of bounds {ref}")


def validate_attachment_mapping(sources: list) -> None:
    for row in sources:
        if not isinstance(row, dict) or row.get("source_id") != "learn.course_files":
            continue
        fields = {
            mapping.get("canonical_field")
            for mapping in row.get("field_mappings", [])
            if isinstance(mapping, dict)
        }
        required = {
            "attachment.source_record_id",
            "attachment.display_name",
            "attachment.mime_type",
            "attachment.byte_size",
            "attachment.attachment_uid",
            "attachment.download_state",
            "attachment.identity_state",
            "attachment.external_transfer_allowed",
            "attachment.source_evidence_id",
        }
        missing = sorted(required - fields)
        check(not missing, f"learn.course_files: attachment-v1 has no mapping for {missing}")


def validate_registry() -> int:
    schema = load(SOURCES / "source-registry.schema.json")
    data = load(SOURCES / "source-registry.json")
    if not isinstance(schema, dict) or not isinstance(data, dict):
        return 0
    schema_validator = validator(schema)
    if schema_validator is not None:
        sample_errors = sorted(schema_validator.iter_errors(data), key=lambda item: list(item.path))
        for item in sample_errors[:12]:
            errors.append(f"source-registry: {list(item.path)}: {item.message}")
        check(not sample_errors, "source-registry: top-level document is not schema-valid")
    sources = data.get("sources")
    check(isinstance(sources, list) and len(sources) >= 20, "source-registry: fewer than 20 sources")
    if not isinstance(sources, list):
        return 0
    ids = [row.get("source_id") for row in sources if isinstance(row, dict)]
    check(len(ids) == len(set(ids)), "source-registry: duplicate source_id")
    for row in sources:
        if not isinstance(row, dict):
            errors.append("source-registry: non-object source")
            continue
        sid = row.get("source_id")
        if row.get("implementation_state") not in ("PLANNED", "BLOCKED"):
            check(bool(row.get("field_mappings")), f"{sid}: implemented read source has no field mappings")
        check(registry_source_invalid(row) is False, f"{sid}: source row has an inconsistent evidence state")
        check(bool(row.get("unresolved") is not None), f"{sid}: unresolved must be an array")
    validate_mapping_targets(sources)
    validate_code_refs(sources)
    validate_attachment_mapping(sources)
    sample = data.get("x-compatibility-sample")
    rejected = data.get("x-rejection-samples")
    if isinstance(schema, dict) and isinstance(schema.get("$defs", {}).get("source"), dict):
        source_schema = {
            "$schema": schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
            "$defs": schema["$defs"],
            "$ref": "#/$defs/source",
        }
        source_validator = validator(source_schema)
        if source_validator is not None and isinstance(sample, dict):
            check(source_validator.is_valid(sample), "source-registry: compatibility sample is invalid")
            check(not registry_source_invalid(sample), "source-registry: compatibility sample is semantically invalid")
        if source_validator is not None and isinstance(rejected, list):
            for index, item in enumerate(rejected):
                value = item.get("value") if isinstance(item, dict) else None
                if not isinstance(value, dict):
                    errors.append(f"source-registry: rejection sample {index} is not an object")
                    continue
                if source_validator.is_valid(value) and not registry_source_invalid(value):
                    errors.append(f"source-registry: rejection sample {index} was accepted")
    check(re.fullmatch(r"[a-f0-9]{40}", str(data.get("x-baseline-commit", ""))) is not None,
          "source-registry: baseline commit must be a full hash")
    return len(sources)


def validate_models() -> int:
    manifest = load(MODELS / "manifest.json")
    if not isinstance(manifest, dict):
        return 0
    models = manifest.get("models")
    check(isinstance(models, list) and len(models) >= 9, "model manifest is incomplete")
    if not isinstance(models, list):
        return 0
    names = [row.get("name") for row in models if isinstance(row, dict)]
    check(len(names) == len(set(names)), "model manifest has duplicate names")
    samples = 0
    rejections = 0
    for row in models:
        if not isinstance(row, dict):
            continue
        schema_path = SOURCES / str(row.get("schema"))
        check(schema_path.is_file(), f"model {row.get('name')}: missing schema {row.get('schema')}")
        if not schema_path.is_file():
            continue
        schema = load(schema_path)
        if not isinstance(schema, dict):
            continue
        check(schema.get("x-contract-version") == row.get("contract"),
              f"model {row.get('name')}: contract version mismatch")
        if row.get("reused_from") == "1.1":
            check(schema.get("x-owner") == "1.1/Hermes",
                  f"model {row.get('name')}: reused contract owner changed")
            continue
        model_samples, model_rejections = validate_schema_contract(
            schema_path, semantic=lambda record: semantic_errors_for(schema_path, record)
        )
        samples += model_samples
        rejections += model_rejections
    return len(models), samples, rejections


def validate_tasks_and_readiness() -> tuple[int, int]:
    registry = load(SOURCES / "source-registry.json")
    source_ids = {row.get("source_id") for row in registry.get("sources", [])} if isinstance(registry, dict) else set()
    tasks = load(SOURCES / "source-tasks.json")
    readiness = load(SOURCES / "adapter-readiness.json")
    task_count = 0
    readiness_count = 0
    if isinstance(tasks, dict):
        rows = tasks.get("tasks")
        check(isinstance(rows, list) and bool(rows), "source-tasks: no tasks")
        if isinstance(rows, list):
            task_count = len(rows)
            task_ids = [row.get("task_id") for row in rows if isinstance(row, dict)]
            check(len(task_ids) == len(set(task_ids)), "source-tasks: duplicate task_id")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tid = row.get("task_id")
                check(row.get("status") in TASK_STATUSES, f"{tid}: invalid task status")
                check(row.get("start_conclusion") in START_CONCLUSIONS, f"{tid}: invalid start conclusion")
                check(bool(row.get("owner")), f"{tid}: missing owner")
                check(bool(row.get("question")), f"{tid}: missing question")
                check(bool(row.get("evidence_refs")), f"{tid}: missing evidence refs")
                check(bool(row.get("forbidden_inferences")), f"{tid}: missing forbidden inferences")
                check(bool(row.get("next_action")), f"{tid}: missing next action")
                check(all(source in source_ids for source in row.get("source_ids", [])),
                      f"{tid}: unknown source id")
                if row.get("status") == "BLOCKED":
                    check(bool(row.get("blocking_reason")), f"{tid}: blocked task has no reason")
    for required in (
        "task.academics.timetable",
        "task.academics.exams",
        "task.academics.grades",
        "task.learn.submission",
        "task.library.renewal",
        "task.model.image_input",
        "task.visitors.form",
    ):
        check(required in {row.get("task_id") for row in (tasks or {}).get("tasks", [])},
              f"source-tasks: missing {required}")
    if isinstance(readiness, dict):
        rows = readiness.get("modules")
        check(isinstance(rows, list) and bool(rows), "adapter-readiness: no modules")
        if isinstance(rows, list):
            readiness_count = len(rows)
            modules = [row.get("module") for row in rows if isinstance(row, dict)]
            check(len(modules) == len(set(modules)), "adapter-readiness: duplicate module")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                mid = row.get("module")
                check(row.get("start_conclusion") in START_CONCLUSIONS, f"{mid}: invalid start conclusion")
                check(isinstance(row.get("contract_ready"), bool), f"{mid}: contract_ready must be boolean")
                check(all(source in source_ids for source in row.get("source_ids", [])),
                      f"{mid}: unknown source id")
                if row.get("start_conclusion", "").startswith("BLOCKED"):
                    check(bool(row.get("blocked_scope")), f"{mid}: blocked module has no blocked scope")
                    check(bool(row.get("next_action")), f"{mid}: blocked module has no next action")
            referenced_sources = {
                source
                for row in rows
                if isinstance(row, dict)
                for source in row.get("source_ids", [])
            }
            missing_sources = sorted(source_ids - referenced_sources)
            check(not missing_sources,
                  f"adapter-readiness: sources without a module conclusion: {missing_sources}")
            by_module = {row.get("module"): row for row in rows if isinstance(row, dict)}
            for mid in ("4.1", "4.5", "5.1", "5.3"):
                check(by_module.get(mid, {}).get("start_conclusion") == "BLOCKED_SOURCE_EVIDENCE",
                      f"adapter-readiness: {mid} must remain blocked on source evidence")
            for mid in ("3.2", "3.3", "6.2"):
                check(by_module.get(mid, {}).get("start_conclusion") == "CONTRACT_READY",
                      f"adapter-readiness: {mid} must be contract-ready")
            for mid in ("4.2", "4.3", "4.4", "5.2", "5.4", "6.1"):
                check(by_module.get(mid, {}).get("start_conclusion") == "CONTRACT_READY_WITH_LIMITS",
                      f"adapter-readiness: {mid} must be contract-ready with limits")
    return task_count, readiness_count


def fixture_schema(path: str):
    data = load(ROOT / path)
    return data if isinstance(data, dict) else None


def validate_fixtures() -> int:
    files = sorted(FIXTURES.glob("*.json"))
    check(bool(files), "source fixtures are missing")
    count = 0
    for path in files:
        data = load(path)
        if not isinstance(data, dict):
            continue
        count += 1
        check(data.get("synthetic") is True, f"{path.name}: fixture must be synthetic")
        check(data.get("fixture_id") == path.stem, f"{path.name}: fixture_id mismatch")

    login = load(FIXTURES / "login-200-no-business.json")
    if isinstance(login, dict):
        expected = login.get("expected", {})
        check(login.get("observations", {}).get("http_status") == 200,
              "login-200 fixture: status must be 200")
        check(expected.get("is_success") is False and expected.get("is_real_empty") is False,
              "login-200 fixture: 200 login shell cannot be a successful empty result")

    same = load(FIXTURES / "same-title-courses.json")
    if isinstance(same, dict):
        schema = fixture_schema("contracts/v0.2/sources/data-models/course.schema.json")
        records = same.get("records", [])
        check(len(records) == 2, "same-title fixture: expected two records")
        if schema and len(records) == 2:
            v = validator(schema)
            for record in records:
                if v is not None:
                    check(v.is_valid(record), f"same-title fixture: invalid record: {first_error(v, record)}")
            check(records[0].get("title") == records[1].get("title"),
                  "same-title fixture: titles must match")
            check(records[0].get("course_uid") != records[1].get("course_uid"),
                  "same-title fixture: course uids must differ")
            check(records[0].get("semester_id") != records[1].get("semester_id"),
                  "same-title fixture: semesters must differ")
            check(same.get("expected", {}).get("merged_by_title") is False,
                  "same-title fixture: must not merge by title")

    missing = load(FIXTURES / "missing-stable-id.json")
    if isinstance(missing, dict):
        schema = fixture_schema("contracts/v0.2/sources/data-models/course.schema.json")
        records = missing.get("records", [])
        check(len(records) == 2, "missing-stable-id fixture: expected two records")
        if schema:
            v = validator(schema)
            for record in records:
                if v is not None:
                    check(v.is_valid(record),
                          f"missing-stable-id fixture: invalid record: {first_error(v, record)}")
                check(record.get("course_uid") is None, "missing-stable-id fixture: course_uid must be null")
                check(record.get("identity_state") == "MISSING_STABLE_ID",
                      "missing-stable-id fixture: wrong identity state")
            check(missing.get("expected", {}).get("merged_by_title") is False,
                  "missing-stable-id fixture: must not merge")
            check(missing.get("expected", {}).get("synthesized_identifier") is False,
                  "missing-stable-id fixture: must not synthesize an identifier")

    states = load(FIXTURES / "empty-vs-next-page-vs-failure.json")
    if isinstance(states, dict):
        schema = fixture_schema("contracts/v0.2/source-evidence.schema.json")
        cases = {row.get("name"): row for row in states.get("cases", []) if isinstance(row, dict)}
        check(set(cases) == {"full_empty", "first_page_empty_but_more", "source_failure"},
              "empty-vs-next-page fixture: wrong case set")
        if schema:
            v = validator(schema)
            for name, row in cases.items():
                evidence = row.get("evidence", {})
                if v is not None:
                    check(v.is_valid(evidence),
                          f"empty-vs-next-page/{name}: invalid evidence: {first_error(v, evidence)}")
        full = cases.get("full_empty", {}).get("evidence", {})
        first = cases.get("first_page_empty_but_more", {}).get("evidence", {})
        failure = cases.get("source_failure", {}).get("evidence", {})
        check(full.get("state") == "EMPTY" and full.get("complete") is True and
              full.get("source_complete") is True and full.get("rows") == [],
              "empty-vs-next-page: full empty fixture is inconsistent")
        check(first.get("state") not in ("EMPTY", "OK") and first.get("complete") is False and
              first.get("source_complete") is False and first.get("upstream_total") == 5,
              "empty-vs-next-page: first-page-empty fixture is inconsistent")
        check(failure.get("state") not in ("EMPTY", "OK") and failure.get("complete") is False and
              failure.get("upstream_total") is None,
              "empty-vs-next-page: failure fixture is inconsistent")

    submissions = load(FIXTURES / "submission-states.json")
    if isinstance(submissions, dict):
        schema = fixture_schema("contracts/v0.2/sources/data-models/submission.schema.json")
        records = submissions.get("records", [])
        if schema:
            v = validator(schema)
            for record in records:
                if v is not None:
                    check(v.is_valid(record),
                          f"submission-states: invalid record: {first_error(v, record)}")
                check(not submission_semantic_errors(record),
                      f"submission-states: semantic error: {'; '.join(submission_semantic_errors(record))}")
        states = [record.get("state") for record in records]
        check(states == ["UPLOADED_STAGING", "DRAFT_SAVED", "SUBMITTED"],
              "submission-states: states are not distinct")
        check(all(record.get("is_final") is (record.get("state") == "SUBMITTED") for record in records),
              "submission-states: only SUBMITTED may be final")
        check(all(record.get("automatic_replay_allowed") is False for record in records),
              "submission-states: automatic replay must be false")

    renewal = load(FIXTURES / "renewal-eligible-failed.json")
    if isinstance(renewal, dict):
        schema = fixture_schema("contracts/v0.2/sources/data-models/renewal.schema.json")
        record = renewal.get("record", {})
        if schema and isinstance(record, dict):
            v = validator(schema)
            if v is not None:
                check(v.is_valid(record), f"renewal fixture: invalid record: {first_error(v, record)}")
            check(not renewal_semantic_errors(record),
                  f"renewal fixture: semantic error: {'; '.join(renewal_semantic_errors(record))}")
        check(record.get("eligibility") == "ELIGIBLE" and record.get("execution_state") == "FAILED",
              "renewal fixture: expected eligible but failed execution")
        check(record.get("due_date_before") == record.get("due_date_after"),
              "renewal fixture: failed execution must not change due date")
        check(record.get("verified") is False, "renewal fixture: failed execution cannot be verified")
        check(record.get("automatic_replay_allowed") is False,
              "renewal fixture: automatic replay must be false")

    image = load(FIXTURES / "model-catalog-mismatch.json")
    if isinstance(image, dict):
        schema = fixture_schema("contracts/v0.2/sources/data-models/model-image-capability.schema.json")
        record = image.get("record", {})
        if schema and isinstance(record, dict):
            v = validator(schema)
            if v is not None:
                check(v.is_valid(record), f"model-image fixture: invalid record: {first_error(v, record)}")
            check(not model_image_semantic_errors(record),
                  f"model-image fixture: semantic error: {'; '.join(model_image_semantic_errors(record))}")
        check(record.get("catalog_support_image") is True and
              record.get("protocol_state") == "UNVERIFIED" and
              record.get("capability_state") == "UNVERIFIED",
              "model-image fixture: catalog flag must remain unverified")
    return count


def validate_module_result() -> bool:
    data = load(SOURCES / "module-result.json")
    if not isinstance(data, dict):
        return False
    check(data.get("module") == "2.3", "module-result: wrong module")
    check(data.get("module_status") == "accepted", "module-result: module is not accepted")
    check(data.get("target_version") == "0.2.0", "module-result: wrong target version")
    delivery_commit = data.get("delivery_commit")
    check(isinstance(delivery_commit, str) and re.fullmatch(r"[a-f0-9]{40}", delivery_commit) is not None,
          "module-result: delivery_commit must be a 40-character SHA")
    checks = data.get("checks", {})
    for key in ("live_campus_calls", "model_calls", "business_submissions"):
        check(checks.get(key) == 0, f"module-result: {key} must be 0")
    paths = data.get("paths", {})
    for key, value in paths.items():
        if key == "test":
            check((ROOT / value).is_file(), f"module-result: missing test path {value}")
        else:
            check((ROOT / value).is_dir(), f"module-result: missing delivery path {value}")
    blockers = data.get("downstream_blockers", [])
    modules = {row.get("module") for row in blockers if isinstance(row, dict)}
    check({"4.1", "4.5", "5.1", "5.3"} <= modules,
          "module-result: downstream blockers are incomplete")
    return True


def validate_privacy() -> None:
    for base in (SOURCES, FIXTURES, DOCS):
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".json", ".md", ".py"}:
                continue
            if path.name == "validate.py":
                # The validator contains the privacy patterns themselves.
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                errors.append(f"cannot read {path.relative_to(ROOT)} for privacy scan")
                continue
            for pattern in PRIVATE_PATTERNS:
                if pattern.search(text):
                    errors.append(f"private-looking value in {path.relative_to(ROOT)}")


def main() -> int:
    registry_count = validate_registry()
    model_count, schema_samples, rejection_samples = validate_models()
    task_count, readiness_count = validate_tasks_and_readiness()
    fixture_count = validate_fixtures()
    module_result = validate_module_result()
    validate_privacy()
    print(json.dumps({
        "ok": not errors,
        "sources": registry_count,
        "models": model_count,
        "tasks": task_count,
        "readiness": readiness_count,
        "fixtures": fixture_count,
        "module_result": module_result,
        "schema_samples": schema_samples,
        "rejection_samples": rejection_samples,
        "errors": errors,
    }, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
