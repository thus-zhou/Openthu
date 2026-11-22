"""Synthetic acceptance tests for module 4.5 (model image input and sources).

Every image is generated locally, every account is a synthetic pseudonym and no
test contacts a model endpoint: the Python transport is a recorder and the Node
transport runs against injected staging files with a fake upstream client.

The suite asserts the behaviours the module promises:

* text requests keep the previous parameters and result type;
* a catalogue flag without recorded transport evidence refuses before any byte
  is read or any request is counted;
* another account, an expired reference or an over-limit selection never reaches
  the transport;
* exactly the named images are staged, and a neighbour file is never opened;
* citations prove identifier registration only, never that a claim is true;
* cancellation, interruption and missing usage keep their partial state and
  never cause a second generation;
* errors, previews and evidence carry no image encoding, prompt text or path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from thu_assistant import model_media, models
from thu_assistant.bridge import CampusError
from thu_assistant.media_assets import MediaAssets, synthetic

ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("THU_NODE", "node")
HAS_NODE = shutil.which(NODE) is not None
node_only = pytest.mark.skipif(not HAS_NODE, reason="Node.js is not installed")
SCOPE = "acct_" + "a" * 16
OTHER_SCOPE = "acct_" + "b" * 16
TASK = "task-model-45"
MODEL = "model-a"

class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class Recorder:
    """A fake model transport: records every command, never reaches a network."""

    def __init__(self, result=None, error=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = {"content": "answer", "usage": None} if result is None else result
        self.error = error

    def __call__(self, command, **arguments):
        self.calls.append((command, dict(arguments)))
        if self.error is not None:
            raise self.error
        return dict(self.result)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def assets(tmp_path, clock):
    value = MediaAssets(tmp_path / "media", clock=clock)
    yield value
    value.close()


def register(assets, tmp_path, *, name="a.png", data=None, purpose="MODEL_INPUT",
             scope=SCOPE, task=TASK, transfer=True):
    path = tmp_path / (secrets.token_hex(4) + "-" + name)
    path.write_bytes(synthetic.png() if data is None else data)
    return assets.register_user_file(
        path,
        account_scope=scope,
        task_id=task,
        purpose=purpose,
        source_evidence_id="local.user_selected",
        external_transfer_allowed=transfer,
        display_name=name,
    )


def binding(assets, tmp_path, *, scope=SCOPE, task=TASK, request_id=None):
    return model_media.MediaBinding(
        assets=assets,
        account_scope=scope,
        task_id=task,
        transport_root=tmp_path / "transport",
        request_id=request_id,
    )


PROFILE = {
    "protocol_id": "madmodel.image.v1",
    "evidence_level": "SYNTHETIC_TRANSPORT",
    "source_id": "model.image_input",
    "synthetic": True,
    "checked_at": "2026-11-17T00:00:00+00:00",
    "max_images": 4,
    "max_image_bytes": 8 * 1024 * 1024,
    "max_total_bytes": 16 * 1024 * 1024,
    "max_pixels": 16_000_000,
}


def registry(*, model=MODEL, **options):
    values = {**PROFILE, **options}
    return model_media.ProtocolRegistry(
        [model_media.ProtocolProfile(model_id=model, **values)]
    )


def reference_id(tmp_path, assets, **options):
    return register(assets, tmp_path, **options).media_id


def image_arg(media_id):
    return {"images": [{"media_id": media_id}]}


# -- text compatibility -------------------------------------------------


def test_text_chat_keeps_its_parameter_shape_and_result_type():
    recorder = Recorder({"content": "ok", "usage": {"total_tokens": 3}})
    out = models.chat(recorder, {"prompt": "x", "model": MODEL})
    assert recorder.calls == [
        ("model", {"messages": [{"role": "user", "content": "x"}], "max_tokens": 1024,
                   "model": MODEL})
    ]
    assert out["content"] == "ok"
    assert out["citation_check"] == "SOURCE_IDS_ONLY_NOT_FACT_CHECKED"
    assert out["output_is_authorization"] is False


def test_multiturn_text_requests_are_unchanged():
    sequence = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]
    recorder = Recorder({"content": "ok"})
    models.chat(recorder, {"messages": sequence})
    assert recorder.calls[0][1]["messages"] == sequence


def test_prepare_refuses_images_instead_of_dropping_them():
    with pytest.raises(CampusError, match="MODEL_IMAGE_REQUIRES_MEDIA_PIPELINE"):
        models.prepare({"prompt": "x", "images": [{"media_id": "media_" + "a" * 12}]})


def test_tool_schema_names_references_not_paths_or_urls():
    assert set(models.PROPERTIES["images"]["items"]["properties"]) == {"media_id"}
    assert models.PROPERTIES["images"]["maxItems"] == 4
    image_schema = json.dumps(models.PROPERTIES["images"], ensure_ascii=False)
    assert "path" not in image_schema and "url" not in image_schema
    assert "base64" not in image_schema


# -- evidence gate ------------------------------------------------------


def test_catalog_flag_without_transport_evidence_reads_nothing(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    reads = assets.audit.source_reads
    recorder = Recorder()
    with pytest.raises(CampusError, match="MODEL_IMAGE_PROTOCOL_UNVERIFIED"):
        models.chat(recorder, {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path))
    assert recorder.calls == []
    assert assets.audit.source_reads == reads


def test_offline_contract_evidence_is_not_transport_evidence(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    with pytest.raises(ValueError):
        model_media.ProtocolProfile(
            model_id=MODEL, **{**PROFILE, "evidence_level": "OFFLINE_CONTRACT"})


def test_a_profile_without_explicit_evidence_is_a_construction_error():
    for missing in ("evidence_level", "source_id", "synthetic", "checked_at"):
        values = {key: value for key, value in PROFILE.items() if key != missing}
        with pytest.raises(TypeError):
            model_media.ProtocolProfile(model_id=MODEL, **values)


def test_profile_defaults_are_conservative_not_the_ceiling():
    values = {
        key: value for key, value in PROFILE.items()
        if not key.startswith("max_")
    }
    profile = model_media.ProtocolProfile(model_id=MODEL, **values)
    assert (profile.max_images, profile.max_image_bytes,
            profile.max_total_bytes, profile.max_pixels) == (
        1, 1024 * 1024, 4 * 1024 * 1024, 4_000_000)
    assert profile.max_images < model_media.MAX_IMAGES
    assert profile.max_image_bytes < model_media.MAX_IMAGE_BYTES


def test_capability_rows_keep_profile_evidence_metadata(tmp_path):
    profile = model_media.ProtocolProfile(
        model_id=MODEL,
        **{**PROFILE, "evidence_level": "LIVE_READ", "synthetic": False,
           "checked_at": "2020-01-01T00:00:00+00:00"},
    )
    (row,) = model_media.capabilities(
        {"models": [{"id": MODEL, "supportImage": True}]},
        protocols=model_media.ProtocolRegistry([profile]))
    assert row["synthetic"] is False
    assert row["checked_at"].startswith("2020-01-01")


def test_capability_rows_never_promote_a_catalog_flag(tmp_path):
    directory = {
        "models": [
            {"id": "model-a", "supportImage": True},
            {"id": "model-b", "supportImage": False},
            {"id": "model-c", "supportImage": None},
            {"id": "model-d", "supportImage": True},
        ]
    }
    rows = model_media.capabilities(directory, protocols=registry())
    by_id = {row["model_id"]: row for row in rows}
    assert by_id["model-a"]["capability_state"] == "AVAILABLE"
    assert by_id["model-b"]["capability_state"] == "UNAVAILABLE"
    assert by_id["model-c"]["capability_state"] == "UNVERIFIED"
    assert by_id["model-d"]["capability_state"] == "UNVERIFIED"
    schema = json.loads(
        (ROOT / "contracts/v0.2/sources/data-models/model-image-capability.schema.json")
        .read_text("utf-8")
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for row in rows:
        assert validator.is_valid(row), list(validator.iter_errors(row))


def test_capability_without_a_registry_is_unverified(tmp_path):
    directory = {"models": [{"id": MODEL, "supportImage": True}]}
    (row,) = model_media.capabilities(directory, protocols=None)
    assert row["capability_state"] == "UNVERIFIED"
    assert row["reason_code"] == "IMAGE_PROTOCOL_NOT_VERIFIED"


# -- selection and scope ------------------------------------------------


def test_other_account_reference_never_reaches_the_transport(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    recorder = Recorder()
    with pytest.raises(CampusError, match="MEDIA_REFERENCE_ACCOUNT_MISMATCH"):
        models.chat(recorder, {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path, scope=OTHER_SCOPE),
                    protocols=registry())
    assert recorder.calls == []


def test_expired_reference_never_reaches_the_transport(tmp_path, assets, clock):
    media_id = reference_id(tmp_path, assets)
    clock.advance(901)
    recorder = Recorder()
    with pytest.raises(CampusError, match="MEDIA_REFERENCE_EXPIRED"):
        models.chat(recorder, {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path), protocols=registry())
    assert recorder.calls == []


def test_duplicate_and_malformed_selections_are_refused(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    recorder = Recorder()
    for images in (
        [{"media_id": media_id}, {"media_id": media_id}],
        [{"media_id": "not-a-media-id"}],
        [{"media_id": media_id, "path": "x"}],
    ):
        with pytest.raises(CampusError):
            models.chat(recorder, {"model": MODEL, "prompt": "x", "images": images},
                        media=binding(assets, tmp_path), protocols=registry())
    assert recorder.calls == []


def test_bare_string_image_references_are_refused(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    request_args = {"model": MODEL, "prompt": "x", "images": [media_id]}
    with pytest.raises(CampusError, match="MODEL_IMAGE_SELECTION_INVALID"):
        model_media.prepare(request_args, media=binding(assets, tmp_path),
                            protocols=registry())


def test_more_than_four_images_are_refused(tmp_path, assets):
    first = reference_id(tmp_path, assets)
    media_ids = [first] + [
        register(assets, tmp_path, name=f"x{index}.png").media_id for index in range(4)
    ]
    recorder = Recorder()
    with pytest.raises(CampusError, match="MODEL_IMAGE_LIMIT_EXCEEDED"):
        models.chat(recorder, {"model": MODEL, "prompt": "x",
                               "images": [{"media_id": item} for item in media_ids]},
                    media=binding(assets, tmp_path), protocols=registry())
    assert recorder.calls == []


def test_stricter_protocol_limits_win(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    recorder = Recorder()
    with pytest.raises(CampusError, match="MODEL_IMAGE_LIMIT_EXCEEDED"):
        models.chat(recorder, {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path),
                    protocols=registry(max_image_bytes=8))
    assert recorder.calls == []


def test_protocol_pixel_limit_is_enforced(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    recorder = Recorder()
    with pytest.raises(CampusError, match="MODEL_IMAGE_LIMIT_EXCEEDED"):
        models.chat(recorder, {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path),
                    protocols=registry(max_pixels=1))
    assert recorder.calls == []


def test_protocol_media_types_are_enforced(tmp_path, assets):
    media_id = reference_id(tmp_path, assets, name="a.jpg", data=synthetic.jpeg())
    recorder = Recorder()
    with pytest.raises(CampusError, match="MODEL_IMAGE_UNSUPPORTED"):
        models.chat(recorder, {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path),
                    protocols=registry(media_types=("image/png",)))
    assert recorder.calls == []


def test_empty_image_selection_is_refused(tmp_path, assets):
    recount = Recorder()
    with pytest.raises(CampusError, match="MODEL_IMAGE_SELECTION_INVALID"):
        models.chat(recount, {"model": MODEL, "prompt": "x", "images": []},
                    media=binding(assets, tmp_path), protocols=registry())
    assert recount.calls == []


def test_protocol_checked_at_must_be_a_full_offset_instant():
    for value in ("yesterday", "2026-11-17", "2026-11-17T12:00:00", ""):
        with pytest.raises(ValueError):
            model_media.ProtocolProfile(
                model_id=MODEL, **{**PROFILE, "checked_at": value})


def test_staging_refuses_bytes_that_do_not_match_the_released_hash(tmp_path):
    class Image:
        media_id = "media_" + "a" * 12
        mime_type = "image/png"
        data = b"synthetic"
        sha256 = "0" * 64

    stage = model_media.TransportStage(tmp_path / "transport")
    with pytest.raises(CampusError, match="MODEL_IMAGE_STAGING_FAILED"):
        stage.write([Image()])
    assert not any((tmp_path / "transport").iterdir())


def test_protocol_cannot_widen_the_frozen_limits():
    with pytest.raises(ValueError):
        model_media.ProtocolProfile(model_id=MODEL, **{**PROFILE, "max_images": 5})
    with pytest.raises(ValueError):
        model_media.ProtocolProfile(
            model_id=MODEL, **{**PROFILE, "max_total_bytes": 32 * 1024 * 1024})


def test_image_call_requires_an_explicit_model(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    recorder = Recorder()
    with pytest.raises(CampusError, match="MODEL_REQUIRED_FOR_IMAGE_INPUT"):
        models.chat(recorder, {"prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path), protocols=registry())
    assert recorder.calls == []


def test_image_call_without_a_media_pipeline_is_refused(tmp_path):
    recorder = Recorder()
    with pytest.raises(CampusError, match="MODEL_IMAGE_INPUT_UNAVAILABLE"):
        models.chat(recorder, {"model": MODEL, "prompt": "x",
                               "images": [{"media_id": "media_" + "a" * 12}]})
    assert recorder.calls == []


# -- the send scope -----------------------------------------------------


def test_only_the_selected_image_is_staged_and_a_neighbour_is_never_read(
    tmp_path, assets
):
    selected = register(assets, tmp_path, name="selected.png")
    register(assets, tmp_path, name="other.png")
    neighbour = tmp_path / "neighbour.png"
    neighbour.write_bytes(synthetic.png(color=(1, 2, 3)))
    before = neighbour.read_bytes()
    reads = assets.audit.source_reads
    recorder = Recorder({"content": "ok"})

    seen = {}

    def watching(command, **arguments):
        assert command == "model"
        seen["command"] = arguments
        seen["staging"] = sorted(
            path.name for path in (tmp_path / "transport").iterdir()
        )
        return {"content": "ok"}

    result = models.chat(watching, {"model": MODEL, "prompt": "只看这一张",
                                    **image_arg(selected.media_id)},
                         media=binding(assets, tmp_path), protocols=registry())

    batch = seen["command"]["image_batch"]
    assert [item["media_id"] for item in batch["images"]] == [selected.media_id]
    assert batch["images"][0]["sha256"] == selected.sha256
    assert len(seen["staging"]) == 1 and seen["staging"][0].endswith(".bin")
    assert not any((tmp_path / "transport").iterdir())
    assert neighbour.read_bytes() == before
    assert assets.audit.source_reads == reads + 1
    assert [item["source_id"] for item in result["media_sources"]] == [selected.media_id]
    assert result["media_sources"][0]["kind"] == "DERIVED_IMAGE"
    assert result["source_map"][0]["kind"] == "DERIVED_IMAGE"
    assert result["input"]["kind"] == "USER_INPUT"
    assert result["output_kind"] == "TEXT_SUMMARY"
    assert result["input"]["chars"] == len("只看这一张")


def test_two_images_keep_the_selection_order(tmp_path, assets):
    first = register(assets, tmp_path, name="1.png")
    second = register(assets, tmp_path, name="2.jpg", data=synthetic.jpeg())
    recorder = Recorder({"content": "ok"})
    result = models.chat(
        recorder,
        {"model": MODEL, "prompt": "two",
         "images": [{"media_id": first.media_id}, {"media_id": second.media_id}]},
        media=binding(assets, tmp_path), protocols=registry())
    batch = recorder.calls[0][1]["image_batch"]
    assert [item["media_id"] for item in batch["images"]] == [first.media_id, second.media_id]
    assert [item["mime_type"] for item in batch["images"]] == ["image/png", "image/jpeg"]
    assert [item["source_id"] for item in result["media_sources"]] == [
        first.media_id, second.media_id]


def test_preview_lists_what_would_be_sent_without_bytes_or_paths(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    request = model_media.prepare(
        {"model": MODEL, "prompt": "preview me", **image_arg(media_id)},
        media=binding(assets, tmp_path), protocols=registry())
    preview = request.preview()
    text = json.dumps(preview, ensure_ascii=False)
    assert preview["image_count"] == 1
    assert preview["images"][0]["media_id"] == media_id
    assert preview["state"] == "PREPARED"
    assert str(tmp_path) not in text
    assert "base64" not in text and "data:image" not in text
    request.cancel()


def test_preview_reports_the_whole_text_scope_and_creates_no_bundle(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    messages = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]
    for _ in range(3):
        request = model_media.prepare(
            {"model": MODEL, "messages": messages, **image_arg(media_id),
             "source_refs": [{"source_id": "notice_1", "text": "课程通知原文"}]},
            media=binding(assets, tmp_path), protocols=registry())
        preview = request.preview()
        assert assets.broker.bundle_count() == 0
    scope = preview["text_scope"]
    actual = sum(len(message["content"]) for message in request.prepared["messages"])
    assert scope["messages"] == len(request.prepared["messages"]) == 4
    assert scope["chars"] == actual
    assert scope["by_role"] == {"system": 1, "user": 2, "assistant": 1}
    assert scope["source_ids"] == ["notice_1"]
    assert scope["per_message"][1] == {"role": "user", "chars": len("第一问")}
    text = json.dumps(preview, ensure_ascii=False)
    assert "第一问" not in text and "课程通知原文" not in text
    assert str(tmp_path) not in text
    request.cancel()


def test_a_send_creates_and_consumes_exactly_one_bundle(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    recorder = Recorder({"content": "ok"})
    models.chat(recorder, {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                media=binding(assets, tmp_path), protocols=registry())
    assert assets.broker.bundle_count() == 1
    assert assets.broker.active() == []


def test_stale_staging_files_are_swept_only_by_a_later_request(tmp_path):
    root = tmp_path / "transport"
    root.mkdir(mode=0o700)
    old = root / ("a" * 32 + ".bin")
    old.write_bytes(b"x")
    stamp = time.time() - 1000
    os.utime(old, (stamp, stamp))
    # Nothing runs in the background: the file survives until a request opens
    # the staging area again.
    assert old.exists()
    model_media.TransportStage(root)
    assert not old.exists()


def test_the_staging_root_must_be_private_and_absolute(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    with pytest.raises(CampusError, match="MODEL_IMAGE_TRANSPORT_UNAVAILABLE"):
        model_media.MediaBinding(
            assets=assets, account_scope=SCOPE, task_id=TASK,
            transport_root=Path("relative"))
    if os.name == "posix":
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o755, exist_ok=True)
        os.chmod(shared, 0o755)
        binding_object = model_media.MediaBinding(
            assets=assets, account_scope=SCOPE, task_id=TASK, transport_root=shared)
        request = model_media.prepare(
            {"model": MODEL, "prompt": "x", **image_arg(media_id)},
            media=binding_object, protocols=registry())
        with pytest.raises(CampusError, match="MODEL_IMAGE_TRANSPORT_UNAVAILABLE"):
            request.send(Recorder())


# -- citations ----------------------------------------------------------


def test_citations_prove_registration_only(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    result = models.chat(
        Recorder({"content": f"见 [[{media_id}]] 与 [[invented_1]]" , "usage": None}),
        {"model": MODEL, "prompt": "x", **image_arg(media_id)},
        media=binding(assets, tmp_path), protocols=registry())
    assert result["citations"] == [
        {"source_id": media_id, "registered": True, "claim_verified": False},
        {"source_id": "invented_1", "registered": False, "claim_verified": False},
    ]
    assert "UNREGISTERED_MODEL_CITATION" in result["warnings"]
    assert result["citation_check"] == "SOURCE_IDS_ONLY_NOT_FACT_CHECKED"
    assert all(item["claim_verified"] is False for item in result["media_sources"])


def test_text_sources_and_images_share_one_source_map(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    result = models.chat(
        Recorder({"content": f"[[notice_1]] [[{media_id}]]"}),
        {"model": MODEL, "prompt": "x", **image_arg(media_id),
         "source_refs": [{"source_id": "notice_1", "text": "指定内容"}]},
        media=binding(assets, tmp_path), protocols=registry())
    kinds = {item["source_id"]: item["kind"] for item in result["source_map"]}
    assert kinds == {"notice_1": "ORIGINAL_QUOTE", media_id: "DERIVED_IMAGE"}
    assert [item["registered"] for item in result["citations"]] == [True, True]
    derived = result["media_sources"][0]
    assert re.fullmatch(r"[a-f0-9]{64}", derived["source_sha256"])
    assert derived["conversion_chain"][0]["from_sha256"] == derived["source_sha256"]


def test_a_converted_image_keeps_the_conversion_chain(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    result = models.chat(
        Recorder({"content": "ok"}),
        {"model": MODEL, "prompt": "x", **image_arg(media_id)},
        media=binding(assets, tmp_path), protocols=registry())
    source = result["media_sources"][0]
    assert source["conversion_chain"]
    assert all(
        step["to_sha256"] == source["sha256"] for step in source["conversion_chain"]
    )
    assert any(step["operation"] == "sha256_verified_at_send"
               for step in source["conversion_chain"])


# -- partial, cancellation and replay -----------------------------------


def test_partial_output_and_unknown_usage_are_preserved(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    error = CampusError("MODEL_STREAM_INTERRUPTED", model_evidence={
        "content": f"部分 [[{media_id}]]", "usage": None, "usage_state": "UNKNOWN",
        "finish_reason": "length", "partial": True, "generation_started": True})
    recorder = Recorder(error=error)
    with pytest.raises(CampusError) as raised:
        models.chat(recorder, {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path), protocols=registry())
    assert len(recorder.calls) == 1
    evidence = raised.value.model_evidence
    assert evidence["content"] == f"部分 [[{media_id}]]"
    assert evidence["usage"] is None and evidence["usage_state"] == "UNKNOWN"
    assert evidence["partial"] is True
    assert evidence["image_input"]["complete"] is False
    assert evidence["image_input"]["count"] == 1
    assert evidence["sources"] == []


def test_a_refused_answer_keeps_the_reported_usage_and_partial_text(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    error = CampusError("MODEL_CONTENT_REFUSED", model_evidence={
        "content": "前一半", "usage": {"total_tokens": 7},
        "usage_state": "REPORTED_BY_UPSTREAM", "finish_reason": "content_filter",
        "generation_started": True})
    with pytest.raises(CampusError) as raised:
        models.chat(Recorder(error=error),
                    {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path), protocols=registry())
    evidence = raised.value.model_evidence
    assert evidence["usage"] == {"total_tokens": 7}
    assert evidence["usage_state"] == "REPORTED_BY_UPSTREAM"
    assert evidence["content"] == "前一半"
    assert evidence["image_input"]["complete"] is False
    assert evidence["media_revoked"] is None


def test_a_truncated_success_marks_the_image_input_incomplete(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    result = models.chat(
        Recorder({"content": "cut", "usage": None, "complete": False,
                  "partial": True, "finish_reason": "length"}),
        {"model": MODEL, "prompt": "x", **image_arg(media_id)},
        media=binding(assets, tmp_path), protocols=registry())
    assert result["complete"] is False
    assert result["partial"] is True
    assert result["finish_reason"] == "length"
    assert result["image_input"]["complete"] is False
    assert result["image_input"]["sends"] == 0


def test_error_evidence_keeps_the_transport_send_count(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    error = CampusError("MODEL_AUTH_REJECTED", model_evidence={
        "content": "", "usage": None, "usage_state": "UNKNOWN",
        "image_input": {
            "count": 1, "total_bytes": 123, "media_ids": [media_id],
            "sha256": ["0" * 64], "protocol_id": "madmodel.image.v1",
            "evidence_level": "SYNTHETIC_TRANSPORT", "sends": 2},
        "generation_started": True})
    with pytest.raises(CampusError) as raised:
        models.chat(Recorder(error=error),
                    {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path), protocols=registry())
    evidence = raised.value.model_evidence["image_input"]
    assert evidence["sends"] == 2
    assert evidence["complete"] is False
    assert evidence["count"] == 1


def test_cancel_revokes_the_media_and_never_generates_twice(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    recorder = Recorder(error=CampusError("CANCELLED", model_evidence={
        "content": "half", "generation_started": True}))
    with pytest.raises(CampusError, match="CANCELLED") as raised:
        models.chat(recorder, {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path), protocols=registry())
    assert raised.value.model_evidence["media_revoked"] is True
    assert len(recorder.calls) == 1
    with pytest.raises(CampusError, match="MEDIA_REFERENCE_REVOKED"):
        models.chat(recorder, {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path), protocols=registry())
    assert len(recorder.calls) == 1


def test_one_grant_authorises_one_send(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    request = model_media.prepare(
        {"model": MODEL, "prompt": "x", **image_arg(media_id)},
        media=binding(assets, tmp_path), protocols=registry())
    recorder = Recorder({"content": "ok"})
    request.send(recorder)
    with pytest.raises(CampusError, match="MEDIA_TRANSFER_NOT_AUTHORIZED"):
        request.send(recorder)
    assert len(recorder.calls) == 1


def test_staging_files_are_removed_when_the_transport_fails(tmp_path, assets):
    media_id = reference_id(tmp_path, assets)
    with pytest.raises(CampusError, match="MODEL_QUOTA_EXCEEDED"):
        models.chat(
            Recorder(error=CampusError("MODEL_QUOTA_EXCEEDED")),
            {"model": MODEL, "prompt": "x", **image_arg(media_id)},
            media=binding(assets, tmp_path), protocols=registry())
    root = tmp_path / "transport"
    assert root.is_dir() and not any(root.iterdir())


# -- leak checks --------------------------------------------------------


def test_errors_and_evidence_carry_no_image_prompt_or_path(tmp_path, assets, caplog):
    media_id = reference_id(tmp_path, assets)
    secret_prompt = "synthetic prompt text 12345"
    with caplog.at_level("DEBUG"):
        with pytest.raises(CampusError) as raised:
            models.chat(
                Recorder(error=CampusError("MODEL_QUOTA_EXCEEDED")),
                {"model": MODEL, "prompt": secret_prompt, **image_arg(media_id)},
                media=binding(assets, tmp_path), protocols=registry())
    evidence = raised.value.model_evidence
    text = json.dumps(evidence, ensure_ascii=False, default=str)
    assert secret_prompt not in str(raised.value)
    assert secret_prompt not in text
    assert "data:image" not in text and "base64" not in text and "/9j/" not in text
    assert str(tmp_path) not in text
    assert evidence["input"]["sha256"] == hashlib.sha256(
        secret_prompt.encode("utf-8")).hexdigest()
    assert all(secret_prompt not in record.getMessage() for record in caplog.records)


def test_image_error_codes_are_the_frozen_media_taxonomy(tmp_path, assets):
    from thu_assistant.media_assets.errors import ERROR_CLASSES

    codes = {item.code for item in ERROR_CLASSES}
    media_id = reference_id(tmp_path, assets)
    with pytest.raises(CampusError) as raised:
        models.chat(Recorder(), {"model": MODEL, "prompt": "x", **image_arg(media_id)},
                    media=binding(assets, tmp_path, scope=OTHER_SCOPE),
                    protocols=registry())
    assert raised.value.code in codes
    assert raised.value.code.startswith("MEDIA_")


def test_non_dict_arguments_keep_the_frozen_input_error():
    for value in (None, "prompt", []):
        with pytest.raises(CampusError, match="INVALID_MODEL_ARGUMENTS"):
            models.chat(lambda *args, **kwargs: None, value)


def test_mcp_model_chat_refuses_images_without_a_media_binding():
    """The shipped MCP path has no MediaBinding yet; it must fail closed.

    It reports MODEL_IMAGE_INPUT_UNAVAILABLE, not PROTOCOL_UNVERIFIED: the
    facade has not injected the media pipeline. 6.1 wires it (see the record).
    """
    from thu_assistant.mcp_stdio import CampusAssistant, McpServer

    recorder = Recorder()
    server = McpServer(CampusAssistant(learning_factory=lambda: None, bridge=recorder))
    response = server.handle({
        "id": 1, "method": "tools/call",
        "params": {"name": "model_chat", "arguments": {
            "prompt": "x", "images": [{"media_id": "media_" + "a" * 12}]}},
    })
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["error"]["code"] == (
        "MODEL_IMAGE_INPUT_UNAVAILABLE")
    assert recorder.calls == []


def test_engine_limits_come_from_the_shared_limits_object():
    from thu_assistant.media_assets import DEFAULT_LIMITS

    assert model_media.MAX_IMAGES == DEFAULT_LIMITS.max_images_per_request
    assert model_media.MAX_IMAGE_BYTES == DEFAULT_LIMITS.max_image_bytes
    assert model_media.MAX_TOTAL_BYTES == DEFAULT_LIMITS.max_total_image_bytes
    assert model_media.MAX_PIXELS == DEFAULT_LIMITS.max_pixels_per_image


def test_the_module_record_maps_every_test():
    record = (ROOT / "plans/v0.2.0/交付/模块结果/4.5.md").read_text("utf-8")
    source = (ROOT / "tests/test_model_media.py").read_text("utf-8")
    defined = set(re.findall(r"^def (test_[a-z0-9_]+)", source, re.M))
    mapped = set(re.findall(r"`(test_[a-z0-9_]+)`", record))
    assert defined <= mapped


def test_every_module_code_is_declared():
    source = "".join(
        (ROOT / path).read_text("utf-8")
        for path in ("src/thu_assistant/model_media.py",
                     "src/thu_assistant/models.py",
                     "src/thu_assistant/models.js")
    )
    emitted = set(re.findall(
        r"(MODEL_IMAGE_[A-Z_]+|MODEL_REQUIRED_FOR_IMAGE_INPUT)", source))
    assert emitted == set(model_media.MODULE_CODES)


# -- Node transport -----------------------------------------------------

NODE_SCRIPT = r"""
'use strict';
const fs=require('node:fs');
const models=require(process.argv[2]);
const job=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const rows=[{id:'model-a',label:'A',supportImage:job.supportImage!==false,thinkingParam:null,
 thinkingField:null,effortOptions:[],availability:'LISTED_NOT_PROBED'}];
const directory={models:rows,checked_at:new Date().toISOString(),freshness:'LIVE'};
const run=async scenario=>{
 let requests=0,tokens=0;const payloads=[];
 const command={model:'model-a',prompt:'synthetic prompt',max_tokens:64};
 if(scenario.assistantLast)command.messages=[{role:'user',content:'q'},{role:'assistant',content:'a'}];
 if(scenario.batch!==undefined)command.image_batch={...scenario.batch,staging_root:job.root};
 const controller=scenario.cancelAfterStart?new AbortController():null;
 const signal=scenario.cancelBefore
  ?AbortSignal.abort(Object.assign(new Error('stop'),{code:'CANCELLED'}))
  :controller?controller.signal:undefined;
 try{
  const result=await models.generate({command,directory,configuredModel:'model-a',
   getToken:async()=>{tokens++;return {token:'fixture',cookie:'fixture'};},
   createClient:()=>({request:async options=>{
    requests++;payloads.push(JSON.parse(JSON.stringify(options.payload)));
    if(scenario.firstUnauthorized&&requests===1)return {type:'upstream-error',status:401};
    options.onOpen();options.onChunk({choices:[{delta:{content:'seen'}}]});
    if(controller)controller.abort(Object.assign(new Error('stop'),{code:'CANCELLED'}));
    return {type:'completion',body:{choices:[{message:{content:'ok'},finish_reason:'stop'}],
     usage:{total_tokens:9}}};
   }}),
   applyThinking:()=>{},signal});
  return {ok:true,requests,tokens,payload:payloads[payloads.length-1],payloads,
   result:{content:result.content,usage:result.usage,image_input:result.image_input,
    refreshed:result.refreshed,automatic_replay:result.automatic_replay}};
 }catch(error){
  return {ok:false,code:error.code,requests,tokens,evidence:error.modelEvidence};
 }
};
(async()=>{
 const output={};
 for(const scenario of job.scenarios)output[scenario.name]=await run(scenario);
 console.log(JSON.stringify(output));
})().catch(error=>{process.exitCode=1;console.error(String(error&&error.code||error));});
"""


def stage_file(root: Path, data: bytes, *, mime="image/png", declared=None):
    token = secrets.token_hex(16)
    (root / f"{token}.bin").write_bytes(data)
    return {
        "token": token,
        "media_id": "media_" + secrets.token_hex(8),
        "mime_type": mime,
        "sha256": declared or hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
    }


def run_node(tmp_path, scenarios, *, support_image=True):
    root = tmp_path / "node-stage"
    root.mkdir(mode=0o700, exist_ok=True)
    script = tmp_path / "transport.js"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps({"root": str(root), "supportImage": support_image,
                    "scenarios": scenarios}), encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(script), str(ROOT / "src/thu_assistant/models.js"), str(job)],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout), root


@node_only
def test_node_transport_refuses_unverified_protocol_and_unsupported_model(tmp_path):
    root = tmp_path / "node-stage"
    root.mkdir(mode=0o700)
    item = stage_file(root, synthetic.png())
    base = {"protocol_id": "madmodel.image.v1", "model_id": "model-a",
            "shape": "OPENAI_CONTENT_PARTS_IMAGE_URL", "evidence_level": "SYNTHETIC_TRANSPORT",
            "max_images": 4, "max_image_bytes": 8 * 1024 * 1024,
            "max_total_bytes": 16 * 1024 * 1024, "images": [item]}
    output, root = run_node(tmp_path, [
        {"name": "unverified", "batch": {**base, "evidence_level": "OFFLINE_CONTRACT"}},
        {"name": "unknown_shape", "batch": {**base, "shape": "GUESSED_SHAPE"}},
    ])
    for name in ("unverified", "unknown_shape"):
        assert output[name]["ok"] is False
        assert output[name]["code"] == "MODEL_IMAGE_PROTOCOL_UNVERIFIED"
        assert output[name]["requests"] == 0 and output[name]["tokens"] == 0
    assert output["unverified"]["evidence"]["generation_started"] is False
    assert output["unverified"]["evidence"]["image_input"]["count"] == 1
    assert (root / f"{item['token']}.bin").exists()


@node_only
def test_node_transport_refuses_a_model_without_image_support(tmp_path):
    root = tmp_path / "node-stage"
    root.mkdir(mode=0o700)
    item = stage_file(root, synthetic.png())
    output, root = run_node(tmp_path, [{
        "name": "unsupported",
        "batch": {"protocol_id": "madmodel.image.v1", "model_id": "model-a",
                  "shape": "OPENAI_CONTENT_PARTS_IMAGE_URL",
                  "evidence_level": "SYNTHETIC_TRANSPORT", "max_images": 4,
                  "max_image_bytes": 8 * 1024 * 1024,
                  "max_total_bytes": 16 * 1024 * 1024, "images": [item]},
    }], support_image=False)
    assert output["unsupported"]["code"] == "MODEL_IMAGE_UNSUPPORTED"
    assert output["unsupported"]["requests"] == 0


@node_only
def test_node_transport_sends_text_first_then_images_in_order(tmp_path):
    root = tmp_path / "node-stage"
    root.mkdir(mode=0o700)
    first = synthetic.png()
    second = synthetic.jpeg()
    items = [stage_file(root, first), stage_file(root, second, mime="image/jpeg")]
    output, root = run_node(tmp_path, [{
        "name": "verified",
        "batch": {"protocol_id": "madmodel.image.v1", "model_id": "model-a",
                  "shape": "OPENAI_CONTENT_PARTS_IMAGE_URL",
                  "evidence_level": "SYNTHETIC_TRANSPORT", "max_images": 4,
                  "max_image_bytes": 8 * 1024 * 1024,
                  "max_total_bytes": 16 * 1024 * 1024, "images": items},
    }])
    result = output["verified"]
    assert result["ok"] and result["requests"] == 1
    content = result["payload"]["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "synthetic prompt"}
    assert [part["type"] for part in content[1:]] == ["image_url", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert result["result"]["image_input"]["count"] == 2
    assert result["result"]["image_input"]["sends"] == 1
    assert result["payload"]["stream"] is True
    assert not any(root.iterdir())


@node_only
def test_node_transport_refuses_a_changed_staging_file(tmp_path):
    root = tmp_path / "node-stage"
    root.mkdir(mode=0o700)
    item = stage_file(root, synthetic.png(), declared="0" * 64)
    output, root = run_node(tmp_path, [{
        "name": "mismatch",
        "batch": {"protocol_id": "madmodel.image.v1", "model_id": "model-a",
                  "shape": "OPENAI_CONTENT_PARTS_IMAGE_URL",
                  "evidence_level": "SYNTHETIC_TRANSPORT", "max_images": 4,
                  "max_image_bytes": 8 * 1024 * 1024,
                  "max_total_bytes": 16 * 1024 * 1024, "images": [item]},
    }])
    assert output["mismatch"]["code"] == "MODEL_IMAGE_STAGING_MISMATCH"
    assert output["mismatch"]["requests"] == 0
    assert output["mismatch"]["evidence"]["generation_started"] is False


@node_only
def test_node_transport_cancel_before_dispatch_sends_nothing(tmp_path):
    root = tmp_path / "node-stage"
    root.mkdir(mode=0o700)
    item = stage_file(root, synthetic.png())
    output, root = run_node(tmp_path, [{
        "name": "cancel_before", "cancelBefore": True,
        "batch": {"protocol_id": "madmodel.image.v1", "model_id": "model-a",
                  "shape": "OPENAI_CONTENT_PARTS_IMAGE_URL",
                  "evidence_level": "SYNTHETIC_TRANSPORT", "max_images": 4,
                  "max_image_bytes": 8 * 1024 * 1024,
                  "max_total_bytes": 16 * 1024 * 1024, "images": [item]},
    }])
    assert output["cancel_before"]["code"] == "CANCELLED"
    assert output["cancel_before"]["requests"] == 0
    # The staged file was never read and is still present for the host cleanup.
    assert (root / f"{item['token']}.bin").exists()


@node_only
def test_node_transport_allows_one_unopened_auth_recovery_without_switching_models(
    tmp_path,
):
    root = tmp_path / "node-stage"
    root.mkdir(mode=0o700)
    items = [stage_file(root, synthetic.png()), stage_file(root, synthetic.jpeg(), mime="image/jpeg")]
    output, root = run_node(tmp_path, [{
        "name": "auth_recovery", "firstUnauthorized": True,
        "batch": {"protocol_id": "madmodel.image.v1", "model_id": "model-a",
                  "shape": "OPENAI_CONTENT_PARTS_IMAGE_URL",
                  "evidence_level": "SYNTHETIC_TRANSPORT", "max_images": 4,
                  "max_image_bytes": 8 * 1024 * 1024,
                  "max_total_bytes": 16 * 1024 * 1024, "images": items},
    }])
    result = output["auth_recovery"]
    assert result["ok"] and result["requests"] == 2 and result["tokens"] == 2
    assert result["result"]["refreshed"] is True
    assert result["result"]["automatic_replay"] is False
    assert result["result"]["image_input"]["sends"] == 2
    assert {payload["model"] for payload in result["payloads"]} == {"model-a"}
    assert result["payloads"][0]["messages"] == result["payloads"][1]["messages"]


@node_only
def test_node_transport_refuses_a_batch_above_the_frozen_ceiling(tmp_path):
    root = tmp_path / "node-stage"
    root.mkdir(mode=0o700)
    output, root = run_node(tmp_path, [{
        "name": "too_wide",
        "batch": {"protocol_id": "madmodel.image.v1", "model_id": "model-a",
                  "shape": "OPENAI_CONTENT_PARTS_IMAGE_URL",
                  "evidence_level": "SYNTHETIC_TRANSPORT", "max_images": 4,
                  "max_image_bytes": 16 * 1024 * 1024,
                  "max_total_bytes": 32 * 1024 * 1024, "images": []},
    }])
    assert output["too_wide"]["code"] == "MODEL_IMAGE_PROTOCOL_UNVERIFIED"
    assert output["too_wide"]["requests"] == 0


@node_only
def test_node_image_message_shape_error_carries_evidence(tmp_path):
    root = tmp_path / "node-stage"
    root.mkdir(mode=0o700)
    item = stage_file(root, synthetic.png())
    output, root = run_node(tmp_path, [{
        "name": "bad_messages", "assistantLast": True,
        "batch": {"protocol_id": "madmodel.image.v1", "model_id": "model-a",
                  "shape": "OPENAI_CONTENT_PARTS_IMAGE_URL",
                  "evidence_level": "SYNTHETIC_TRANSPORT", "max_images": 4,
                  "max_image_bytes": 8 * 1024 * 1024,
                  "max_total_bytes": 16 * 1024 * 1024, "images": [item]},
    }])
    result = output["bad_messages"]
    assert result["code"] == "MODEL_IMAGE_MESSAGES_INVALID"
    assert result["requests"] == 0
    assert result["evidence"]["generation_started"] is False
    assert result["evidence"]["image_input"]["count"] == 1


@node_only
def test_node_transport_cancel_after_start_never_replays(tmp_path):
    root = tmp_path / "node-stage"
    root.mkdir(mode=0o700)
    item = stage_file(root, synthetic.png())
    output, root = run_node(tmp_path, [{
        "name": "cancel_after", "cancelAfterStart": True,
        "batch": {"protocol_id": "madmodel.image.v1", "model_id": "model-a",
                  "shape": "OPENAI_CONTENT_PARTS_IMAGE_URL",
                  "evidence_level": "SYNTHETIC_TRANSPORT", "max_images": 4,
                  "max_image_bytes": 8 * 1024 * 1024,
                  "max_total_bytes": 16 * 1024 * 1024, "images": [item]},
    }])
    result = output["cancel_after"]
    assert result["code"] == "CANCELLED"
    assert result["requests"] == 1
    assert result["evidence"]["image_input"]["sends"] == 1
    assert result["evidence"]["automatic_replay"] is False
