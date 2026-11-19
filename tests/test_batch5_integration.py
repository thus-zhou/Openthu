"""Batch-5 integration seams.

Each module proves itself in isolation.  These cases cover the seams the batch
integration owner is responsible for:

* every error code a batch-5 module can emit is registered in the frozen
  ``error-classification-v1`` registry *and* mirrored in ``source_state``, so the
  future facade (6.1) classifies it instead of collapsing it to ``UNKNOWN``;
* a module error keeps its code and category through the shared boundary;
* the batch-5 module records, the ownership table and the batch gate agree, and
  an accepted batch-5 gate names a full integration commit while batch 6 is the
  single ready batch;
* 5.3 registers a new ``learn`` service in the shared 4.4 action engine, and that
  service must never inherit the sports write switch (5.2) just because it is
  not the library;
* 5.1's renewal operation declares only the prechecks it can actually verify, and
  the engine's declared-precheck rule does not loosen any other gated write.

No test contacts a campus service, a model endpoint or the internet.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from thu_assistant import actions, source_state, submissions
from thu_assistant.bridge import CampusError

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "plans" / "v0.2.0" / "交付"
BATCH5 = ("5.1", "5.2", "5.3", "5.4")

# Module-specific error-code prefixes.  A new code in these modules must be
# registered; a token that is a flag, warning, enum or record name is listed in
# _NON_CODE_TOKENS with the reason it is not an error code.
_PREFIXES = {
    "5.1": ("RENEWAL_", "LIBRARY_RENEW_"),
    "5.2": ("SPORTS_",),
    "5.3": ("SUBMISSION_",),
    "5.4": (
        "EVENT_",
        "AGENDA_",
        "SNAPSHOT_",
        "EXPORT_",
        "INVALID_ACADEMIC_SEMESTER",
        "ACADEMICS_SOURCE_BLOCKED",
    ),
}

_SOURCES = {
    "5.1": [
        "src/thu_assistant/library_actions.py",
        "src/thu_assistant/library_contracts.py",
        "src/thu_assistant/library_wire.py",
    ]
    + sorted(str(path.relative_to(ROOT)) for path in (ROOT / "src/thu_assistant/renewal").glob("*.py")),
    "5.2": [
        "src/thu_assistant/sports_actions.py",
        "src/thu_assistant/sports_transport.py",
    ],
    "5.3": sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "src/thu_assistant/submissions").glob("*.py")
    ),
    "5.4": [
        "src/thu_assistant/todos.py",
        "src/thu_assistant/events.py",
        "src/thu_assistant/snapshot_diff.py",
        "src/thu_assistant/calendar_export.py",
    ],
}

# Strings that look like codes but are not errors: plan flags, warning tags,
# excluded reasons, enum names and record identifiers.
_NON_CODE_TOKENS = {
    # 5.1 risk flags carried in the preview, not raised errors.
    "RENEWAL_ELIGIBILITY_CHANGED",
    "RENEWAL_ELIGIBILITY_UNVERIFIED",
    "RENEWAL_SOURCE_CONTRACT_UNVERIFIED",
    # 5.3 enum name, warning tag and reconciliation limitation tag.
    "SUBMISSION_STATES",
    "SUBMISSION_VERSION_UNVERIFIED",
    "SUBMISSION_RECORD_MISSING",
    # 5.4 purpose name, enum sets, exclusion/warning tags.
    "EVENT_CURSOR",
    "EVENT_KINDS",
    "EXPORT_STATES",
    "EVENT_CONVERSION_EXCLUDED",
    "SNAPSHOT_SEQUENCE_PERSIST_FAILED",
}


def _frozen_codes() -> dict[str, str]:
    contract = json.loads(
        (ROOT / "contracts" / "v0.2" / "error-classification.json").read_text(
            encoding="utf-8"
        )
    )
    return contract["codes"]


def _string_constants(paths: list[str]) -> set[str]:
    found: set[str] = set()
    for relative in paths:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return found


def _module_codes(module: str) -> set[str]:
    found = _string_constants(_SOURCES[module])
    prefixes = _PREFIXES[module]
    return {
        value
        for value in found
        if value.startswith(prefixes) and value not in _NON_CODE_TOKENS
    }


def test_every_batch5_module_code_is_registered_and_mirrored() -> None:
    frozen = _frozen_codes()
    for module in BATCH5:
        codes = _module_codes(module)
        assert codes, f"{module}: no module codes found"
        missing = sorted(codes - set(frozen))
        assert not missing, f"{module} emits unregistered codes: {missing}"
        for code in codes:
            assert frozen[code] in source_state.CATEGORIES, code
            assert source_state.ERROR_CODES[code] == frozen[code], code

    # 5.3 keeps its own table; use it as the authoritative cross-check.
    for code, rule in submissions.CODES.items():
        assert frozen[code] == rule[0], code
        assert source_state.ERROR_CODES[code] == rule[0], code


def test_batch5_errors_keep_their_code_and_category_through_the_shared_boundary() -> None:
    for code, category in (
        ("RENEWAL_OVERDUE", "INPUT"),
        ("RENEWAL_CONTRACT_EVIDENCE_UNVERIFIED", "UPSTREAM_CONTRACT"),
        ("LIBRARY_RENEW_DISABLED", "PERMISSION"),
        ("SPORTS_DISPATCH_UNOBSERVED", "NETWORK"),
        ("SPORTS_SLOT_UNAVAILABLE", "CONFLICT"),
        ("SUBMISSION_FILE_CHANGED", "CONFLICT"),
        ("SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED", "NOT_IMPLEMENTED"),
        ("EVENT_CURSOR_EXPIRED", "COMPLETENESS"),
        ("EXPORT_SOURCE_INCOMPLETE", "COMPLETENESS"),
    ):
        public = source_state.public_error(code)
        assert public["code"] == code
        assert public["category"] == category
        assert "UNKNOWN" not in public["code"]

        classified = source_state.classify_error(CampusError(code))
        assert classified["code"] == code
        assert classified["category"] == category


def test_batch5_records_ownership_and_gate_agree() -> None:
    ownership = json.loads((DELIVERY / "模块负责表.json").read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in ownership["modules"] if row["id"] in BATCH5}
    assert set(rows) == set(BATCH5)

    gate = json.loads((DELIVERY / "批次门禁.json").read_text(encoding="utf-8"))
    by_batch = {item["batch"]: item for item in gate["batches"]}
    batch = by_batch[5]
    assert all(status == "accepted" for status in batch["module_status"].values())

    for module in BATCH5:
        record = json.loads(
            (DELIVERY / "模块结果" / f"{module}.json").read_text(encoding="utf-8")
        )
        assert record["status"] == "accepted", module
        assert record["batch"] == 5, module
        assert rows[module]["status"] == "accepted", module
        assert record["delivery_commit"] == rows[module]["delivery_commit"], module
        assert rows[module]["accepted_at"], module

    if batch["status"] == "accepted":
        assert batch["integration_commit"]
        assert len(batch["integration_commit"]) == 40
        assert batch["accepted_at"]
        # The gate is a monotonic accepted prefix plus at most one ready batch;
        # a later batch may already be accepted by the time this suite runs.
        assert by_batch[6]["status"] in ("ready", "accepted")
        if by_batch[6]["status"] == "ready":
            assert by_batch[7]["status"] == "locked"


def test_new_learn_service_never_inherits_the_sports_write_switch(monkeypatch) -> None:
    monkeypatch.setenv("THU_SPORTS_WRITE", "1")
    monkeypatch.setenv("THU_SPORTS_READ", "1")
    monkeypatch.setenv("THU_LIBRARY_WRITE", "1")
    monkeypatch.setenv("THU_LIBRARY_RENEW", "1")

    saved = dict(actions.REGISTRY)
    saved_adapters = set(actions.VERIFIED_ADAPTERS)
    try:
        submissions.register()
        contract = actions.REGISTRY[("learn", "submit_assignment")]
        # 5.3 owns THU_LEARN_SUBMIT; the shared engine must not enable it.
        assert contract["enabled"] is False
        described = {
            row["operation"]: row for row in actions.describe_actions("learn")
        }
        assert described["submit_assignment"]["enabled"] is False
        assert actions.action_enabled("learn") is False
        # The two services that do have switches keep their behaviour.
        assert actions.action_enabled("sports") is True
        assert actions.action_enabled("library") is True
    finally:
        for key in set(actions.REGISTRY) - set(saved):
            del actions.REGISTRY[key]
        actions.REGISTRY.update(saved)
        actions.VERIFIED_ADAPTERS.clear()
        actions.VERIFIED_ADAPTERS.update(saved_adapters)


def test_renewal_declares_only_the_prechecks_it_can_verify() -> None:
    renewal = actions.REGISTRY[("library", "renew_loan")]
    assert renewal["prechecks"] == ("eligibility", "rules")
    assert renewal["enabled"] is False
    assert renewal["disabled_reason"] == "SOURCE_CONTRACT_UNVERIFIED"

    for (service, operation), contract in actions.REGISTRY.items():
        if (service, operation) == ("library", "renew_loan"):
            continue
        if service in {"library", "sports"}:
            # 5.1 narrowed the engine to each operation's declared axes; every
            # other gated write still declares the full four-axis set.
            assert contract["prechecks"] == actions.CHECKS, (service, operation)

    described = {row["operation"]: row for row in actions.describe_actions("library")}
    assert described["renew_loan"]["enabled"] is False
    assert described["renew_loan"]["reason"] == "SOURCE_CONTRACT_UNVERIFIED"
    assert described["renew_loan"]["reconciliation_scope"]
