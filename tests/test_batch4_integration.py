"""Batch-4 integration seams.

Module tests prove each module in isolation.  These cases cover the seams the
integration owner is responsible for:

* every error code a batch-4 module can emit is registered in the frozen
  ``error-classification-v1`` registry *and* in the ``source_state`` mirror, so
  the future facade (6.1) classifies it instead of collapsing it to ``UNKNOWN``;
* a module error travelling through ``source_state.classify_error`` keeps its
  code and category;
* the module records, the ownership table and the batch gate agree on status
  and delivery commits;
* a course-resource media reference (4.2, purpose ``COURSE_RESOURCE``) is not
  reusable as a model input (4.5, purpose ``MODEL_INPUT``): the 3.3 registry
  refuses the cross-purpose read before any byte is staged.

No test contacts a campus service, a model endpoint or the internet.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from thu_assistant import model_media, source_state
from thu_assistant.academics import errors as academics_errors
from thu_assistant.bridge import CampusError
from thu_assistant.media_assets import errors as media_errors
from thu_assistant.resources import errors as resource_errors

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "plans" / "v0.2.0" / "交付"
BATCH4 = ("4.1", "4.2", "4.3", "4.4", "4.5")


def _frozen_codes() -> dict[str, str]:
    contract = json.loads(
        (ROOT / "contracts" / "v0.2" / "error-classification.json").read_text(
            encoding="utf-8"
        )
    )
    return contract["codes"]


def _module_codes() -> dict[str, dict[str, str]]:
    media = {
        klass.code: klass.category
        for klass in vars(media_errors).values()
        if isinstance(klass, type)
        and issubclass(klass, Exception)
        and getattr(klass, "code", None)
    }
    return {
        "4.1": {code: rule[0] for code, rule in academics_errors.CODES.items()},
        "4.2": {
            klass.code: klass.category for klass in resource_errors.ERROR_CLASSES
        },
        "4.5": {code: _MODEL_CATEGORIES[code] for code in model_media.MODULE_CODES},
        # 4.5 re-raises 3.3 media errors on its own channel, so the batch-4
        # registry covers the whole media taxonomy as well.
        "3.3/4.5 media": media,
    }


# Model image codes reuse the 3.3 media taxonomy: malformed caller input is
# INPUT, an unwired capability is NOT_IMPLEMENTED, local staging faults are
# INTERNAL.
_MODEL_CATEGORIES = {
    "MODEL_REQUIRED_FOR_IMAGE_INPUT": "INPUT",
    "MODEL_IMAGE_REQUIRES_MEDIA_PIPELINE": "NOT_IMPLEMENTED",
    "MODEL_IMAGE_SELECTION_INVALID": "INPUT",
    "MODEL_IMAGE_LIMIT_EXCEEDED": "INPUT",
    "MODEL_IMAGE_UNSUPPORTED": "INPUT",
    "MODEL_IMAGE_PROTOCOL_UNVERIFIED": "NOT_IMPLEMENTED",
    "MODEL_IMAGE_INPUT_UNAVAILABLE": "NOT_IMPLEMENTED",
    "MODEL_IMAGE_TRANSPORT_UNAVAILABLE": "NOT_IMPLEMENTED",
    "MODEL_IMAGE_STAGING_FAILED": "INTERNAL",
    "MODEL_IMAGE_STAGING_MISMATCH": "INTERNAL",
    "MODEL_IMAGE_MESSAGES_INVALID": "INPUT",
}


def test_every_batch4_module_code_is_registered_and_mirrored():
    frozen = _frozen_codes()
    for module, codes in _module_codes().items():
        missing = sorted(set(codes) - set(frozen))
        assert not missing, f"{module} emits unregistered codes: {missing}"
        for code, category in codes.items():
            assert frozen[code] == category, f"{code} registered as {frozen[code]}"
            assert source_state.ERROR_CODES[code] == category
            assert category in source_state.CATEGORIES


def test_batch4_errors_keep_their_code_and_category_through_the_shared_boundary():
    for code, category in (
        ("ACADEMICS_INVALID_SEMESTER", "INPUT"),
        ("ACADEMICS_GRADE_CACHE_FORBIDDEN", "PERMISSION"),
        ("RESOURCE_PLAN_EXPIRED", "COMPLETENESS"),
        ("RESOURCE_STORE_UNAVAILABLE", "INTERNAL"),
        ("MODEL_IMAGE_PROTOCOL_UNVERIFIED", "NOT_IMPLEMENTED"),
        ("MODEL_IMAGE_STAGING_MISMATCH", "INTERNAL"),
    ):
        public = source_state.public_error(code)
        assert public["code"] == code
        assert public["category"] == category
        assert "UNKNOWN" not in public["code"]

    academics = source_state.classify_error(
        academics_errors.AcademicsError("ACADEMICS_INVALID_SEMESTER")
    )
    assert academics["code"] == "ACADEMICS_INVALID_SEMESTER"
    assert academics["category"] == "INPUT"

    resource = source_state.classify_error(
        resource_errors.ResourceStoreUnavailable("synthetic")
    )
    assert resource["code"] == "RESOURCE_STORE_UNAVAILABLE"
    assert resource["category"] == "INTERNAL"

    model = source_state.classify_error(CampusError("MODEL_IMAGE_UNSUPPORTED"))
    assert model["code"] == "MODEL_IMAGE_UNSUPPORTED"
    assert model["category"] == "INPUT"


def test_batch4_records_ownership_and_gate_agree():
    ownership = json.loads(
        (DELIVERY / "模块负责表.json").read_text(encoding="utf-8")
    )
    rows = {row["id"]: row for row in ownership["modules"] if row["id"] in BATCH4}
    assert set(rows) == set(BATCH4)

    gate = json.loads((DELIVERY / "批次门禁.json").read_text(encoding="utf-8"))
    batch = next(item for item in gate["batches"] if item["batch"] == 4)
    if batch["status"] == "accepted":
        assert batch["integration_commit"]
        assert len(batch["integration_commit"]) == 40
        assert all(
            status == "accepted" for status in batch["module_status"].values()
        )

    for module in BATCH4:
        record = json.loads(
            (DELIVERY / "模块结果" / f"{module}.json").read_text(encoding="utf-8")
        )
        assert record["status"] == "accepted"
        assert rows[module]["status"] == "accepted"
        assert record["delivery_commit"] == rows[module]["delivery_commit"], module
        assert rows[module]["accepted_at"]
        assert record["batch"] == 4


def test_course_resource_reference_is_not_a_model_input(tmp_path):
    from thu_assistant.media_assets import MediaAssets
    from thu_assistant.media_assets.errors import MediaReferencePurposeMismatch
    from thu_assistant.resources import ResourceCenter
    from thu_assistant.secure_store import SecureStore, create_key_file, load_provider

    import synthetic_resources as synthetic

    key_path = tmp_path / "keys" / "master.key"
    create_key_file(key_path, "windows-dpapi" if os.name == "nt" else "file-key")
    store = SecureStore(load_provider(key_path), tmp_path / "data")
    assets = MediaAssets(tmp_path / "media")
    centre = ResourceCenter(store, "synthetic-student", media_assets=assets)
    centre.ingest(
        {
            "adapter_version": "test",
            "checked_at": "2026-09-02T09:00:00+08:00",
            "semester": "2026-2027-1",
            "course_id": None,
            "full_semester": True,
            "complete": True,
            "stale_courses": [],
            "source_errors": [],
            "warnings": [],
            "candidates": [synthetic.candidate(record_id="f1", title="讲义.pdf")],
        }
    )
    plan = centre.preview_download(
        semester="2026-2027-1",
        selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"),
    )
    centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher()
    )
    record = centre.download_records("2026-2027-1")["records"][0]
    media_id = record["media_asset_id"]

    manifest = assets.manifest(
        [media_id],
        account_scope=centre.media_scope,
        task_id=record["download_id"],
        purpose="COURSE_RESOURCE",
    )
    assert manifest["count"] == 1
    assert manifest["assets"][0]["mime_type"] == "application/pdf"

    with pytest.raises(MediaReferencePurposeMismatch):
        assets.manifest(
            [media_id],
            account_scope=centre.media_scope,
            task_id=record["download_id"],
            purpose="MODEL_INPUT",
        )

    # The refusal happens on the registry lookup, before any staging directory
    # or outbound byte exists.
    assert not (tmp_path / "transport").exists()
    assets.close()
