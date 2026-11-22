"""Synthetic acceptance tests for module 3.3 (local media privacy).

Every file is generated locally, every account is a synthetic pseudonym and no
test contacts a campus service, a model endpoint or the internet. The suite
asserts the behaviours the module promises:

* content decides the type, not an extension or a browser-declared MIME;
* image bytes are re-encoded, metadata is dropped and derived hashes are used;
* oversized or undecodable data fails without a sendable half-product;
* a reference is bound to one account, one task, one purpose and one expiry;
* nothing leaves the process without a single-use grant for that request;
* logs, errors and manifests carry no path, no encoded image and no credential.
"""
from __future__ import annotations

import ast
import errno
import hashlib
import io
import json
import logging
import os
import socket
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from thu_assistant.media_assets import MediaAssets, MediaLimits
from thu_assistant.media_assets import boundary, sniffing, synthetic
from thu_assistant.media_assets.boundary import sanitize_display_name
from thu_assistant.media_assets.errors import (
    ERROR_CLASSES,
    MediaBundleLimit,
    MediaCancelled,
    MediaContentForbidden,
    MediaContentMismatch,
    MediaDecodeFailed,
    MediaEmptyFile,
    MediaFileNotFound,
    MediaFileUnreadable,
    MediaInvalidArgument,
    MediaNotRegularFile,
    MediaPathInvalid,
    MediaProcessingTimeout,
    MediaReadFailed,
    MediaReferenceAccountMismatch,
    MediaReferenceConsumed,
    MediaReferenceCorrupt,
    MediaReferenceExpired,
    MediaReferenceNotFound,
    MediaReferencePurposeMismatch,
    MediaReferenceRevoked,
    MediaReferenceTaskMismatch,
    MediaResourceLimit,
    MediaSizeLimit,
    MediaSourceKindInvalid,
    MediaStateUnavailable,
    MediaTempInUse,
    MediaTempInsecure,
    MediaTransferNotAuthorized,
    MediaTypeUnsupported,
    MediaWriteFailed,
)
from thu_assistant.media_assets.registry import MediaStateStore

ROOT = Path(__file__).resolve().parents[1]
MEDIA_SCHEMA = json.loads(
    (ROOT / "contracts" / "v0.2" / "media-reference.schema.json").read_text("utf-8")
)
ERROR_CONTRACT = json.loads(
    (ROOT / "contracts" / "v0.2" / "error-classification.json").read_text("utf-8")
)
PACKAGE = ROOT / "src" / "thu_assistant" / "media_assets"

SCOPE = "acct_" + "a" * 24
OTHER_SCOPE = "acct_" + "b" * 24
TASK = "task-synthetic-1"
OTHER_TASK = "task-synthetic-2"
EVIDENCE = "local.synthetic_fixture"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def assets(tmp_path: Path, clock: Clock) -> MediaAssets:
    return MediaAssets(tmp_path / "temp", clock=clock)


@pytest.fixture()
def sources(tmp_path: Path) -> Path:
    directory = tmp_path / "sources"
    directory.mkdir()
    return directory


def register_image(
    assets: MediaAssets,
    path: Path,
    *,
    scope: str = SCOPE,
    task: str = TASK,
    purpose: str = "MODEL_INPUT",
    transfer: bool = True,
    clock: Clock | None = None,
    retention: str | None = None,
):
    if clock is not None:
        assets.clock = clock
    return assets.register_synthetic_file(
        path,
        account_scope=scope,
        task_id=task,
        purpose=purpose,
        source_evidence_id=EVIDENCE,
        external_transfer_allowed=transfer,
        retention=retention,
    )


LOCK_NAME = ".media-lock"


def media_files(root: Path) -> list[Path]:
    """Derived files, excluding the single-owner lock marker."""
    return [item for item in root.iterdir() if item.name != LOCK_NAME]


def png_file(sources: Path, name: str = "synthetic.png", data: bytes | None = None) -> Path:
    return synthetic.write_file(sources, name, data if data is not None else synthetic.png())


# ---------------------------------------------------------------------------
# error taxonomy
# ---------------------------------------------------------------------------


def test_every_media_error_uses_a_frozen_category_policy():
    codes = [cls.code for cls in ERROR_CLASSES]
    assert len(codes) == len(set(codes))
    assert all(code.startswith("MEDIA_") for code in codes)
    categories = ERROR_CONTRACT["categories"]
    for cls in ERROR_CLASSES:
        policy = categories[cls.category]
        assert cls.automatic_replay_allowed is False, cls.code
        assert cls.retryable == policy["retryable"], cls.code
        assert cls.user_action == policy["user_action"], cls.code


def test_error_message_redacts_path_like_detail():
    error = MediaPathInvalid("/home/example/private/file.png")
    assert "/home" not in str(error)
    assert error.as_error()["code"] == "MEDIA_PATH_INVALID"


# ---------------------------------------------------------------------------
# registration, descriptor and manifest
# ---------------------------------------------------------------------------


def test_user_selected_png_registers_and_descriptor_matches_contract(assets, sources):
    path = png_file(sources)
    reference = assets.register_user_file(
        path,
        account_scope=SCOPE,
        task_id=TASK,
        purpose="USER_REVIEW",
        source_evidence_id=EVIDENCE,
    )
    descriptor = reference.to_reference()
    validator = Draft202012Validator(MEDIA_SCHEMA, format_checker=FormatChecker())
    assert validator.is_valid(descriptor), list(validator.iter_errors(descriptor))
    assert descriptor["kind"] == "IMAGE"
    assert descriptor["mime_type"] == "image/png"
    assert descriptor["storage_purpose"] == "MEDIA"
    assert descriptor["external_transfer_allowed"] is False
    assert descriptor["display_name"] == "synthetic.png"


def test_descriptor_and_manifest_contain_no_local_path(assets, sources, tmp_path):
    path = png_file(sources)
    reference = register_image(assets, path)
    descriptor = json.dumps(reference.to_reference(), ensure_ascii=False)
    manifest = json.dumps(
        assets.manifest([reference.media_id], account_scope=SCOPE, task_id=TASK),
        ensure_ascii=False,
    )
    for payload in (descriptor, manifest):
        assert str(tmp_path) not in payload
        assert "base64" not in payload
        assert "iVBOR" not in payload
        assert "/home/" not in payload


def test_manifest_entry_carries_purpose_source_and_transform(assets, sources):
    path = synthetic.write_file(sources, "course.png", synthetic.png_with_gps())
    reference = assets.register_synthetic_file(
        path,
        account_scope=SCOPE,
        task_id=TASK,
        purpose="COURSE_RESOURCE",
        source_evidence_id="learn.course_files",
        kind="IMAGE",
    )
    manifest = assets.manifest(
        [reference.media_id], account_scope=SCOPE, task_id=TASK, purpose="COURSE_RESOURCE"
    )
    entry = manifest["assets"][0]
    assert entry["asset_id"] == reference.media_id
    assert entry["account_scope"] == SCOPE
    assert entry["purpose"] == "COURSE_RESOURCE"
    assert entry["mime_type"] == "image/png"
    assert entry["byte_size"] == reference.byte_size
    assert len(entry["sha256"]) == 64
    assert entry["expires_at"]
    assert entry["transform"]["metadata_removed"] is True
    assert entry["source"]["kind"] == "SYNTHETIC"
    assert entry["source"]["source_evidence_id"] == "learn.course_files"
    assert "path" not in json.dumps(entry)


def test_attachment_pdf_registers_with_null_dimensions(assets, sources):
    path = synthetic.write_file(sources, "handout.pdf", synthetic.pdf_document())
    reference = assets.register_downloaded_attachment(
        path,
        kind="ATTACHMENT",
        account_scope=SCOPE,
        task_id=TASK,
        purpose="COURSE_RESOURCE",
        service_domain="LEARN",
        source_evidence_id="learn.course_files",
        declared_mime="application/pdf",
    )
    descriptor = reference.to_reference()
    validator = Draft202012Validator(MEDIA_SCHEMA, format_checker=FormatChecker())
    assert validator.is_valid(descriptor)
    assert descriptor["kind"] == "ATTACHMENT"
    assert descriptor["width"] is None
    assert descriptor["pixel_count"] is None
    assert descriptor["display_name"] == "handout.pdf"


def test_display_name_cannot_smuggle_a_path(assets, sources):
    path = png_file(sources)
    reference = assets.register_user_file(
        path,
        account_scope=SCOPE,
        task_id=TASK,
        display_name="C:" + "\\" + "Users" + "\\" + "synthetic" + "\\photo.png",
    )
    assert reference.to_reference()["display_name"] == "photo.png"


# ---------------------------------------------------------------------------
# file boundary
# ---------------------------------------------------------------------------


def test_directory_is_rejected(assets, sources):
    directory = sources / "folder"
    directory.mkdir()
    with pytest.raises(MediaNotRegularFile):
        register_image(assets, directory)


def test_symlink_is_rejected(assets, sources, tmp_path):
    target = png_file(sources)
    link = tmp_path / "link.png"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform/user cannot create symbolic links")
    with pytest.raises(MediaPathInvalid):
        register_image(assets, link)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO; Windows has no unprivileged equivalent")
def test_special_file_is_rejected(assets, sources):
    fifo = sources / "pipe.png"
    os.mkfifo(fifo)
    try:
        with pytest.raises(MediaNotRegularFile):
            register_image(assets, fifo)
    finally:
        fifo.unlink(missing_ok=True)


def test_empty_file_is_rejected(assets, sources):
    path = synthetic.write_file(sources, "empty.png", b"")
    with pytest.raises(MediaEmptyFile):
        register_image(assets, path)


def test_oversized_file_is_rejected_before_decode(assets, sources):
    path = synthetic.write_file(
        sources, "big.png", synthetic.png_magic_padding(8 * 1024 * 1024 + 16)
    )
    with pytest.raises(MediaSizeLimit):
        register_image(assets, path)
    assert media_files(assets.temp.root) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows ACLs are covered by 2.2")
def test_unreadable_file_is_rejected(assets, sources):
    if os.geteuid() == 0:
        pytest.skip("root can read any file, so the permission path cannot be exercised")
    path = png_file(sources)
    path.chmod(0o000)
    try:
        with pytest.raises(MediaFileUnreadable):
            register_image(assets, path)
    finally:
        path.chmod(0o600)


def test_url_in_path_field_is_rejected(assets):
    with pytest.raises(MediaSourceKindInvalid):
        assets.register_user_file(
            "https://example.invalid/photo.png",
            account_scope=SCOPE,
            task_id=TASK,
        )


def test_nul_byte_in_path_is_rejected(assets):
    with pytest.raises(MediaPathInvalid):
        assets.register_user_file("synthetic\x00.png", account_scope=SCOPE, task_id=TASK)


def test_missing_file_is_rejected(assets, sources):
    with pytest.raises(MediaFileNotFound):
        register_image(assets, sources / "absent.png")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; NTFS ACLs are not mode-based")
def test_world_readable_temp_root_is_refused(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    os.chmod(root, 0o755)
    with pytest.raises(MediaTempInsecure):
        MediaAssets(root)


def test_relative_temp_root_is_refused():
    with pytest.raises(MediaTempInsecure):
        MediaAssets("relative/temp")


def test_platform_stricter_limits_are_adopted(tmp_path, sources):
    limits = MediaLimits().stricter(
        {"max_bytes_per_image": 1024, "mime_types": ["image/png"]}
    )
    assert limits.max_image_bytes == 1024
    assert limits.image_mime_types == ("image/png",)
    assets = MediaAssets(tmp_path / "temp", limits=limits)
    path = synthetic.write_file(sources, "photo.jpg", synthetic.jpeg())
    with pytest.raises(MediaTypeUnsupported):
        register_image(assets, path)
    big = synthetic.write_file(sources, "big.png", synthetic.png_magic_padding(2048))
    with pytest.raises(MediaSizeLimit):
        register_image(assets, big)


# ---------------------------------------------------------------------------
# content over extension
# ---------------------------------------------------------------------------


def test_png_renamed_as_jpeg_is_rejected(assets, sources):
    path = synthetic.write_file(sources, "photo.jpg", synthetic.png())
    with pytest.raises(MediaContentMismatch):
        register_image(assets, path)


def test_html_renamed_as_png_is_rejected(assets, sources):
    path = synthetic.write_file(sources, "page.png", synthetic.html_document())
    with pytest.raises(MediaContentForbidden):
        register_image(assets, path)


def test_executable_renamed_as_pdf_is_rejected(assets, sources):
    path = synthetic.write_file(sources, "handout.pdf", synthetic.executable_stub())
    with pytest.raises(MediaContentForbidden):
        assets.register_downloaded_attachment(
            path, kind="ATTACHMENT", account_scope=SCOPE, task_id=TASK
        )


def test_svg_named_as_png_is_rejected(assets, sources):
    path = synthetic.write_file(sources, "diagram.png", synthetic.svg_document())
    with pytest.raises(MediaContentForbidden):
        register_image(assets, path)


def test_declared_mime_conflict_is_rejected(assets, sources):
    path = synthetic.write_file(sources, "handout.pdf", synthetic.pdf_document())
    with pytest.raises(MediaContentMismatch):
        assets.register_downloaded_attachment(
            path,
            kind="ATTACHMENT",
            account_scope=SCOPE,
            task_id=TASK,
            declared_mime="image/png",
        )


def test_ooxml_container_is_accepted_as_attachment(assets, sources):
    path = synthetic.write_file(sources, "notes.docx", synthetic.zip_document())
    reference = assets.register_downloaded_attachment(
        path, kind="ATTACHMENT", account_scope=SCOPE, task_id=TASK,
        purpose="COURSE_RESOURCE", service_domain="LEARN",
    )
    assert reference.mime_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_markdown_keeps_its_declared_text_subtype(assets, sources):
    path = synthetic.write_file(sources, "notes.md", synthetic.plain_text())
    reference = assets.register_downloaded_attachment(
        path, kind="ATTACHMENT", account_scope=SCOPE, task_id=TASK,
        purpose="COURSE_RESOURCE",
    )
    assert reference.mime_type == "text/markdown"


def test_pdf_named_as_docx_is_rejected(assets, sources):
    path = synthetic.write_file(sources, "notes.docx", synthetic.pdf_document())
    with pytest.raises(MediaContentMismatch):
        assets.register_downloaded_attachment(
            path, kind="ATTACHMENT", account_scope=SCOPE, task_id=TASK
        )


def test_generic_declared_mime_is_ignored(assets, sources):
    path = synthetic.write_file(sources, "handout.pdf", synthetic.pdf_document())
    reference = assets.register_downloaded_attachment(
        path,
        kind="ATTACHMENT",
        account_scope=SCOPE,
        task_id=TASK,
        declared_mime="application/octet-stream",
    )
    assert reference.mime_type == "application/pdf"


# ---------------------------------------------------------------------------
# image sanitisation
# ---------------------------------------------------------------------------


def test_gps_metadata_is_removed_from_derived_png(assets, sources):
    path = synthetic.write_file(sources, "located.png", synthetic.png_with_gps())
    original = path.read_bytes()
    from PIL import Image

    assert Image.open(path).getexif().get_ifd(0x8825), "fixture must carry GPS data"
    reference = register_image(assets, path)
    derived = reference.path.read_bytes()
    reopened = Image.open(reference.path)
    assert len(reopened.getexif()) == 0
    assert "exif" not in reopened.info
    assert b"exif" not in derived[:4096].lower()
    assert reference.sha256 == hashlib.sha256(derived).hexdigest()
    assert reference.sha256 != reference.source_sha256
    assert path.read_bytes() == original


def test_device_metadata_and_comment_are_removed_from_derived_jpeg(assets, sources):
    path = synthetic.write_file(sources, "camera.jpg", synthetic.jpeg_with_device_metadata())
    reference = register_image(assets, path)
    from PIL import Image

    reopened = Image.open(reference.path)
    assert len(reopened.getexif()) == 0
    assert not {"exif", "comment", "icc_profile"} & set(reopened.info)
    assert b"SyntheticCam" not in reference.path.read_bytes()


def test_orientation_is_applied_and_tag_removed(assets, sources):
    path = synthetic.write_file(
        sources, "rotated.png", synthetic.png_with_gps(orientation=6)
    )
    reference = register_image(assets, path)
    descriptor = reference.to_reference()
    assert (descriptor["width"], descriptor["height"]) == (20, 40)
    from PIL import Image

    assert 0x0112 not in Image.open(reference.path).getexif()


def test_small_file_with_huge_dimensions_fails_within_limits(assets, sources):
    path = synthetic.write_file(sources, "bomb.png", synthetic.png_header_only(20000, 20000))
    assert path.stat().st_size < 4096
    with pytest.raises(MediaResourceLimit):
        register_image(assets, path)
    assert media_files(assets.temp.root) == []


def test_pixel_limit_is_enforced_with_a_stricter_limit(tmp_path, sources):
    limits = MediaLimits(max_pixels_per_image=1000)
    assets = MediaAssets(tmp_path / "temp", limits=limits)
    ok = synthetic.write_file(sources, "ok.png", synthetic.png(40, 20))
    assert register_image(assets, ok).pixel_count == 800
    too_big = synthetic.write_file(sources, "too-big.png", synthetic.png(40, 30))
    with pytest.raises(MediaResourceLimit):
        register_image(assets, too_big)


def test_truncated_jpeg_is_rejected(assets, sources):
    path = synthetic.write_file(sources, "truncated.jpg", synthetic.truncated_jpeg())
    with pytest.raises(MediaDecodeFailed):
        register_image(assets, path)


def test_animated_webp_is_rejected(assets, sources):
    from PIL import Image, features

    if not features.check("webp"):
        pytest.skip("Pillow build has no WebP support on this platform")
    try:
        data = synthetic.animated_webp()
    except (OSError, ValueError):
        pytest.skip("this Pillow build cannot write animated WebP")
    assert getattr(Image.open(io.BytesIO(data)), "n_frames", 1) > 1
    path = synthetic.write_file(sources, "animated.webp", data)
    with pytest.raises(MediaTypeUnsupported):
        register_image(assets, path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; NTFS ACLs are not mode-based")
def test_derived_file_is_private_and_inside_the_temp_root(assets, sources):
    path = png_file(sources)
    reference = register_image(assets, path)
    assert st_mode_ok(reference.path)
    assert reference.path.parent == assets.temp.root


def st_mode_ok(path: Path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# reference scope
# ---------------------------------------------------------------------------


def test_other_account_cannot_resolve(assets, sources):
    reference = register_image(assets, png_file(sources))
    with pytest.raises(MediaReferenceAccountMismatch):
        assets.read_bytes(reference.media_id, account_scope=OTHER_SCOPE, task_id=TASK)
    assert assets.read_bytes(reference.media_id, account_scope=SCOPE, task_id=TASK)


def test_other_task_cannot_resolve(assets, sources):
    reference = register_image(assets, png_file(sources))
    with pytest.raises(MediaReferenceTaskMismatch):
        assets.read_bytes(reference.media_id, account_scope=SCOPE, task_id=OTHER_TASK)


def test_purpose_binding_is_enforced(assets, sources):
    reference = register_image(assets, png_file(sources), purpose="COURSE_RESOURCE")
    with pytest.raises(MediaReferencePurposeMismatch):
        assets.read_bytes(
            reference.media_id, account_scope=SCOPE, task_id=TASK, purpose="MODEL_INPUT"
        )


def test_expired_reference_is_rejected(assets, sources, clock):
    reference = register_image(assets, png_file(sources), clock=clock)
    clock.advance(seconds=901)
    with pytest.raises(MediaReferenceExpired):
        assets.read_bytes(reference.media_id, account_scope=SCOPE, task_id=TASK)


def test_unknown_reference_is_rejected(assets):
    with pytest.raises(MediaReferenceNotFound):
        assets.read_bytes("media_" + "0" * 24, account_scope=SCOPE, task_id=TASK)


def test_tampered_derived_file_is_detected(assets, sources):
    reference = register_image(assets, png_file(sources))
    payload = bytearray(reference.path.read_bytes())
    payload[-1] ^= 0xFF
    reference.path.write_bytes(bytes(payload))
    with pytest.raises(MediaReferenceCorrupt):
        assets.read_bytes(reference.media_id, account_scope=SCOPE, task_id=TASK)


def test_cancel_task_revokes_and_removes(assets, sources):
    first = register_image(assets, png_file(sources, "one.png"))
    second = register_image(assets, png_file(sources, "two.png"))
    report = assets.cancel_task(TASK)
    assert report["revoked"] == 2
    assert report["removed"] == 2
    assert not first.path.exists() and not second.path.exists()
    with pytest.raises(MediaReferenceRevoked):
        assets.read_bytes(first.media_id, account_scope=SCOPE, task_id=TASK)
    assert assets.registry.active_paths() == set()


def test_source_info_exposes_provenance_without_a_path(assets, sources):
    path = synthetic.write_file(sources, "course.png", synthetic.png())
    reference = assets.register_synthetic_file(
        path,
        account_scope=SCOPE,
        task_id=TASK,
        purpose="COURSE_RESOURCE",
        source_evidence_id="learn.course_files",
    )
    info = assets.source_info(reference.media_id, account_scope=SCOPE, task_id=TASK)
    assert info["source"]["source_evidence_id"] == "learn.course_files"
    assert info["source"]["source_sha256"] == reference.source_sha256
    assert str(assets.temp.root) not in json.dumps(info)


# ---------------------------------------------------------------------------
# model bundle and capability
# ---------------------------------------------------------------------------


def test_bundle_preview_lists_what_would_be_sent(assets, sources):
    reference = register_image(assets, png_file(sources))
    bundle = assets.prepare_model_bundle(
        [reference.media_id],
        account_scope=SCOPE,
        task_id=TASK,
        request_id="req-synthetic-1",
        model_id="synthetic-model-a",
        prompt_text="请描述这张合成图片",
    )
    preview = bundle.preview()
    assert preview["image_count"] == 1
    assert preview["total_image_bytes"] == reference.byte_size
    assert preview["model_id"] == "synthetic-model-a"
    assert preview["text"]["chars"] == len("请描述这张合成图片")
    assert preview["images"][0]["media_id"] == reference.media_id
    assert "data" not in json.dumps(preview)
    assert str(assets.temp.root) not in json.dumps(preview)


def test_bundle_requires_explicit_external_transfer_flag(assets, sources):
    reference = register_image(assets, png_file(sources), transfer=False)
    with pytest.raises(MediaTransferNotAuthorized):
        assets.prepare_model_bundle(
            [reference.media_id],
            account_scope=SCOPE,
            task_id=TASK,
            request_id="req-synthetic-1",
            model_id="synthetic-model-a",
        )


def test_bundle_rejects_a_non_image_reference(assets, sources):
    path = synthetic.write_file(sources, "handout.pdf", synthetic.pdf_document())
    reference = assets.register_downloaded_attachment(
        path, kind="ATTACHMENT", account_scope=SCOPE, task_id=TASK, purpose="MODEL_INPUT"
    )
    with pytest.raises(MediaInvalidArgument):
        assets.prepare_model_bundle(
            [reference.media_id],
            account_scope=SCOPE,
            task_id=TASK,
            request_id="req-synthetic-1",
            model_id="synthetic-model-a",
        )


def test_bundle_count_limit(assets, sources):
    ids = [
        register_image(assets, png_file(sources, f"image-{index}.png")).media_id
        for index in range(5)
    ]
    with pytest.raises(MediaBundleLimit):
        assets.prepare_model_bundle(
            ids,
            account_scope=SCOPE,
            task_id=TASK,
            request_id="req-synthetic-1",
            model_id="synthetic-model-a",
        )


def test_bundle_total_bytes_limit(tmp_path, sources):
    limits = MediaLimits(max_total_image_bytes=150)
    assets = MediaAssets(tmp_path / "temp", limits=limits)
    ids = [
        register_image(assets, png_file(sources, f"image-{index}.png")).media_id
        for index in range(2)
    ]
    with pytest.raises(MediaBundleLimit):
        assets.prepare_model_bundle(
            ids,
            account_scope=SCOPE,
            task_id=TASK,
            request_id="req-synthetic-1",
            model_id="synthetic-model-a",
        )


def test_grant_requires_the_matching_request_id(assets, sources):
    reference = register_image(assets, png_file(sources))
    bundle = assets.prepare_model_bundle(
        [reference.media_id],
        account_scope=SCOPE,
        task_id=TASK,
        request_id="req-synthetic-1",
        model_id="synthetic-model-a",
    )
    with pytest.raises(MediaTransferNotAuthorized):
        assets.authorize_model_send(bundle.bundle_id, request_id="req-other-1")
    grant = assets.authorize_model_send(bundle.bundle_id, request_id="req-synthetic-1")
    with pytest.raises(MediaTransferNotAuthorized):
        assets.consume_model_bundle(grant, request_id="req-other-1")


def test_grant_is_single_use(assets, sources):
    reference = register_image(assets, png_file(sources))
    bundle = assets.prepare_model_bundle(
        [reference.media_id],
        account_scope=SCOPE,
        task_id=TASK,
        request_id="req-synthetic-1",
        model_id="synthetic-model-a",
    )
    grant = assets.authorize_model_send(bundle.bundle_id, request_id="req-synthetic-1")
    images = assets.consume_model_bundle(grant, request_id="req-synthetic-1")
    assert images[0].mime_type == "image/png"
    assert hashlib.sha256(images[0].data).hexdigest() == reference.sha256
    with pytest.raises(MediaReferenceConsumed):
        assets.consume_model_bundle(grant, request_id="req-synthetic-1")


def test_capability_probe_never_reads_or_sends_media(assets, sources):
    register_image(assets, png_file(sources))
    before = assets.audit_report()
    report = assets.capability(
        model_id="synthetic-model-a",
        catalog_state="LISTED",
        catalog_support_image=True,
        protocol_state="UNVERIFIED",
        evidence_level="OFFLINE_CONTRACT",
    )
    assert report["capability_state"] == "UNVERIFIED"
    assert report["protocol_state"] == "UNVERIFIED"
    assert assets.audit_report() == before


def test_capability_requires_transport_evidence_for_available(assets):
    report = assets.capability(
        model_id="synthetic-model-a",
        catalog_state="LISTED",
        catalog_support_image=True,
        protocol_state="VERIFIED",
        evidence_level="OFFLINE_CONTRACT",
    )
    assert report["capability_state"] == "UNVERIFIED"
    verified = assets.capability(
        model_id="synthetic-model-a",
        catalog_state="LISTED",
        catalog_support_image=True,
        protocol_state="VERIFIED",
        evidence_level="SYNTHETIC_TRANSPORT",
    )
    assert verified["capability_state"] == "AVAILABLE"


# ---------------------------------------------------------------------------
# failure, cleanup and cancellation
# ---------------------------------------------------------------------------


def test_disk_full_leaves_no_half_product(tmp_path, sources):
    def failing_write(fd: int, data) -> int:
        raise OSError(errno.ENOSPC, "no space left")

    assets = MediaAssets(tmp_path / "temp", write_hook=failing_write)
    with pytest.raises(MediaWriteFailed):
        register_image(assets, png_file(sources))
    assert media_files(assets.temp.root) == []
    assert len(assets.registry) == 0
    assert assets.audit_report()["temp_writes"] == 0


def test_cancel_during_processing_leaves_nothing(assets, sources):
    calls = {"count": 0}

    def cancel() -> bool:
        calls["count"] += 1
        return calls["count"] >= 2

    with pytest.raises(MediaCancelled):
        assets.register_synthetic_file(
            png_file(sources),
            account_scope=SCOPE,
            task_id=TASK,
            purpose="MODEL_INPUT",
            source_evidence_id=EVIDENCE,
            external_transfer_allowed=True,
            cancel=cancel,
        )
    assert media_files(assets.temp.root) == []
    assert len(assets.registry) == 0


def test_processing_deadline_aborts_without_output(assets, sources):
    with pytest.raises(MediaProcessingTimeout):
        assets.register_synthetic_file(
            png_file(sources),
            account_scope=SCOPE,
            task_id=TASK,
            purpose="MODEL_INPUT",
            source_evidence_id=EVIDENCE,
            external_transfer_allowed=True,
            deadline=time.monotonic() - 1,
        )
    assert media_files(assets.temp.root) == []
    assert len(assets.registry) == 0


def test_cleanup_keeps_active_references_and_removes_orphans(assets, sources):
    reference = register_image(assets, png_file(sources))
    orphan = assets.temp.root / ("media-" + "1" * 32 + ".png")
    orphan.write_bytes(b"orphan")
    stale_part = assets.temp.root / ("media-" + "2" * 32 + ".png.part")
    stale_part.write_bytes(b"partial")
    report = assets.cleanup()
    assert report.removed == 2
    assert reference.path.exists()
    assert not orphan.exists() and not stale_part.exists()


def test_close_removes_session_files(assets, sources):
    reference = register_image(assets, png_file(sources))
    assets.close()
    assert not reference.path.exists()


def test_cleanup_never_deletes_foreign_files(assets, sources):
    foreign = assets.temp.root / "notes.txt"
    foreign.write_text("user file", encoding="utf-8")
    report = assets.cleanup()
    assert report.removed == 0
    assert report.kept == 1          # the foreign file; the lock marker is not a file count
    assert foreign.exists()


@pytest.mark.skipif(os.name != "posix", reason="descriptor accounting needs /proc")
def test_reads_do_not_leak_file_descriptors(assets, sources):
    path = png_file(sources)
    register_image(assets, path)
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(10):
        assets.register_user_file(
            path, account_scope=SCOPE, task_id=TASK, purpose="USER_REVIEW"
        )
    after = len(os.listdir("/proc/self/fd"))
    assert after <= before + 1


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------


def test_media_package_imports_no_network_client():
    banned = {"socket", "ssl", "urllib", "http", "requests", "aiohttp", "ftplib", "smtplib"}
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            assert not (set(names) & banned), f"{path.name} imports {names}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "print", f"{path.name} prints to the terminal"


def test_preparation_makes_no_network_connection(assets, sources, monkeypatch):
    attempts: list[object] = []

    def guard(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("media preparation attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", guard, raising=True)
    monkeypatch.setattr(socket, "create_connection", guard, raising=True)
    reference = register_image(assets, png_file(sources))
    bundle = assets.prepare_model_bundle(
        [reference.media_id],
        account_scope=SCOPE,
        task_id=TASK,
        request_id="req-synthetic-1",
        model_id="synthetic-model-a",
    )
    grant = assets.authorize_model_send(bundle.bundle_id, request_id="req-synthetic-1")
    assets.consume_model_bundle(grant, request_id="req-synthetic-1")
    assert attempts == []
    assert assets.audit_report()["network_attempts"] == 0


def test_errors_and_logs_carry_no_path_or_image_bytes(assets, sources, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    bad = synthetic.write_file(sources, "fake.png", synthetic.html_document())
    with pytest.raises(MediaContentForbidden) as caught:
        register_image(assets, bad)
    message = str(caught.value)
    assert str(tmp_path) not in message
    assert "fake.png" not in message
    assert "<html" not in message
    assert "iVBOR" not in message and "base64" not in message
    assert str(tmp_path) not in caplog.text
    assert caught.value.as_error()["automatic_replay_allowed"] is False


# ---------------------------------------------------------------------------
# optional encrypted persistence
# ---------------------------------------------------------------------------


def test_encrypted_state_roundtrip_and_cross_account_rejection(tmp_path, sources):
    from thu_assistant.secure_store import (
        SecureStore,
        create_key_file,
        load_provider,
    )

    backend = "windows-dpapi" if os.name == "nt" else "file-key"
    key_path = tmp_path / "keys" / "master.key"
    create_key_file(key_path, backend)
    store = SecureStore(load_provider(key_path), tmp_path / "data")
    root = tmp_path / "temp"

    assets = MediaAssets(root, state_store=store)
    reference = register_image(assets, png_file(sources), retention="EXPLICIT_DELETE")
    record_id = assets.state_record_id(SCOPE)
    assets.close()

    reloaded = MediaAssets(root, state_store=store)
    assert reloaded.restore_state(SCOPE) == 1
    data = reloaded.read_bytes(reference.media_id, account_scope=SCOPE, task_id=TASK)
    assert hashlib.sha256(data).hexdigest() == reference.sha256

    stored = store.read_json(
        record_id,
        purpose="MEDIA",
        account_domain="CAMPUS",
        service_domain="LOCAL",
        account=SCOPE,
    )
    payload = json.dumps(stored, ensure_ascii=False)
    assert str(tmp_path) not in payload
    assert "file_name" in payload

    with pytest.raises(MediaStateUnavailable):
        MediaStateStore(store, record_id=record_id).load(account=OTHER_SCOPE)


# ---------------------------------------------------------------------------
# restart behaviour
# ---------------------------------------------------------------------------


def _run_media_script(script: str, *args: str) -> str:
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


REGISTER_SCRIPT = """
import sys
from pathlib import Path
from thu_assistant.media_assets import MediaAssets, synthetic
root = Path(sys.argv[1])
sources = root / "sources"
sources.mkdir()
path = synthetic.write_file(sources, "one.png", synthetic.png())
assets = MediaAssets(root / "temp")
ref = assets.register_user_file(
    path,
    account_scope="acct_" + "f" * 24,
    task_id="task-restart-1",
    purpose="COURSE_RESOURCE",
    retention=sys.argv[2],
)
print(ref.media_id)
"""


def test_explicit_retention_survives_a_restart(tmp_path):
    _run_media_script(REGISTER_SCRIPT, str(tmp_path), "EXPLICIT_DELETE")
    leftovers = media_files(tmp_path / "temp")
    assert len(leftovers) == 1
    assert leftovers[0].stat().st_size > 0


def test_session_retention_is_removed_at_process_exit(tmp_path):
    _run_media_script(REGISTER_SCRIPT, str(tmp_path), "SESSION")
    leftovers = media_files(tmp_path / "temp")
    assert leftovers == []


STORE_REGISTER_SCRIPT = """
import sys
from pathlib import Path
from thu_assistant.media_assets import MediaAssets, synthetic
from thu_assistant.secure_store import SecureStore, create_key_file, load_provider
root = Path(sys.argv[1])
if not (root / "keys" / "master.key").exists():
    create_key_file(root / "keys" / "master.key", sys.argv[2])
store = SecureStore(load_provider(root / "keys" / "master.key"), root / "data")
sources = root / "sources"
sources.mkdir(exist_ok=True)
path = synthetic.write_file(sources, "one.png", synthetic.png())
assets = MediaAssets(root / "temp", state_store=store)
ref = assets.register_user_file(
    path,
    account_scope="acct_" + "f" * 24,
    task_id="task-restart-1",
    purpose="COURSE_RESOURCE",
    retention="EXPLICIT_DELETE",
)
print(ref.media_id)
"""

STORE_RESTORE_SCRIPT = """
import sys
from pathlib import Path
from thu_assistant.media_assets import MediaAssets
from thu_assistant.secure_store import SecureStore, load_provider
root = Path(sys.argv[1])
store = SecureStore(load_provider(root / "keys" / "master.key"), root / "data")
assets = MediaAssets(root / "temp", state_store=store)
restored = assets.restore_state("acct_" + "f" * 24)
data = assets.read_bytes(sys.argv[2], account_scope="acct_" + "f" * 24,
                         task_id="task-restart-1")
print(restored, len(data))
"""


def test_encrypted_state_survives_a_restart(tmp_path):
    backend = "windows-dpapi" if os.name == "nt" else "file-key"
    media_id = _run_media_script(STORE_REGISTER_SCRIPT, str(tmp_path), backend)
    restored, size = _run_media_script(
        STORE_RESTORE_SCRIPT, str(tmp_path), media_id
    ).split()
    assert int(restored) == 1
    assert int(size) > 0


def test_pending_cleanup_lists_retention_without_paths(assets, sources, clock):
    live = register_image(assets, png_file(sources, "live.png"), clock=clock)
    expired = register_image(
        assets, png_file(sources, "expired.png"), clock=clock, retention="EXPLICIT_DELETE"
    )
    clock.advance(seconds=901)
    rows = {row["asset_id"]: row for row in assets.pending_cleanup()}
    assert set(rows) == {live.media_id, expired.media_id}
    assert rows[live.media_id]["retention"] == "SESSION"
    assert rows[expired.media_id]["retention"] == "EXPLICIT_DELETE"
    assert rows[expired.media_id]["expired"] is True
    assert all(row["derived_present"] for row in rows.values())
    assert str(assets.temp.root) not in json.dumps(rows)


def test_cleanup_removes_expired_reference_files(assets, sources, clock):
    reference = register_image(
        assets, png_file(sources), clock=clock, retention="EXPLICIT_DELETE"
    )
    clock.advance(seconds=901)
    assert assets.pending_cleanup()[0]["expired"] is True
    report = assets.cleanup()
    assert report.removed == 1
    assert not reference.path.exists()


# ---------------------------------------------------------------------------
# audit regressions (P1/P2 from .local/audits/3.3-audit-20261117.md)
# ---------------------------------------------------------------------------


def test_scope_none_is_not_a_media_account(assets, sources):
    """P1-1: 2.2's 'no account' sentinel must never bind a media scope."""
    path = png_file(sources)
    with pytest.raises(MediaInvalidArgument):
        assets.register_user_file(path, account_scope="none", task_id=TASK)
    assert media_files(assets.temp.root) == []
    assert len(assets.registry) == 0


def test_second_instance_cannot_own_the_same_temp_root(tmp_path, sources):
    """P1-2: an unowned sweep could delete another instance's live files."""
    root = tmp_path / "shared"
    first = MediaAssets(root)
    reference = register_image(first, png_file(sources), retention="EXPLICIT_DELETE")
    with pytest.raises(MediaTempInUse):
        MediaAssets(root)
    assert reference.path.exists()
    first.close()
    second = MediaAssets(root)          # the lock is released on close
    assert reference.path.exists()      # EXPLICIT_DELETE survives the owner exit
    second.close()


def test_state_record_id_is_scoped_to_the_controlled_root(tmp_path):
    first = MediaAssets(tmp_path / "a")
    second = MediaAssets(tmp_path / "b")
    try:
        assert first.state_record_id(SCOPE) != second.state_record_id(SCOPE)
        assert first.state_record_id(SCOPE).startswith(f"media-{SCOPE}-")
    finally:
        first.close()
        second.close()


def test_media_assets_rejects_wrong_limit_or_clock_types(tmp_path):
    with pytest.raises(MediaInvalidArgument):
        MediaAssets(tmp_path / "t1", limits={"max_image_bytes": 1})  # type: ignore[arg-type]
    with pytest.raises(MediaInvalidArgument):
        MediaAssets(tmp_path / "t2", clock="now")  # type: ignore[arg-type]


def test_body_read_failure_is_normalised_on_both_paths(tmp_path, sources):
    """P2-1: a mid-read EIO must not escape as a bare OSError."""
    image = synthetic.write_file(sources, "one.png", synthetic.png())
    pdf = synthetic.write_file(sources, "one.pdf", synthetic.pdf_document())
    real_read = boundary.os.read

    def fail_after_head(fd, size):
        if fail_after_head.calls:
            raise OSError(errno.EIO, "simulated body failure")
        fail_after_head.calls = 1
        return real_read(fd, size)

    for kind, path in (("IMAGE", image), ("ATTACHMENT", pdf)):
        fail_after_head.calls = 0
        boundary.os.read = fail_after_head
        assets = MediaAssets(tmp_path / f"read-{kind}")
        try:
            with pytest.raises(MediaReadFailed):
                if kind == "IMAGE":
                    assets.register_user_file(path, account_scope=SCOPE, task_id=TASK)
                else:
                    assets.register_downloaded_attachment(
                        path, kind="ATTACHMENT", account_scope=SCOPE, task_id=TASK
                    )
        finally:
            boundary.os.read = real_read
            assets.close()
        assert media_files(tmp_path / f"read-{kind}") == []


def test_cancel_after_the_derived_write_leaves_no_file(tmp_path, sources):
    """P2-2: the post-write gate must discard the derived file."""
    written = {"done": False}
    real_write = os.write

    def watching_write(fd, data):
        count = real_write(fd, data)
        written["done"] = True
        return count

    assets = MediaAssets(tmp_path / "temp", write_hook=watching_write)
    with pytest.raises(MediaCancelled):
        assets.register_user_file(
            png_file(sources),
            account_scope=SCOPE,
            task_id=TASK,
            purpose="MODEL_INPUT",
            source_evidence_id=EVIDENCE,
            external_transfer_allowed=True,
            cancel=lambda: written["done"],
        )
    assert media_files(assets.temp.root) == []
    assert len(assets.registry) == 0


def test_capability_report_satisfies_its_contract(assets):
    """P2-3: the public capability object must pass the frozen schema."""
    schema = json.loads(
        (ROOT / "contracts" / "v0.2" / "sources" / "data-models"
         / "model-image-capability.schema.json").read_text("utf-8")
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    report = assets.capability(
        model_id="synthetic-model-a",
        catalog_state="LISTED",
        catalog_support_image=True,
        protocol_state="UNVERIFIED",
        evidence_level="OFFLINE_CONTRACT",
        warning="x" * 400,
    )
    assert len(report["warning"]) == 300
    assert validator.is_valid(report), list(validator.iter_errors(report))
    with pytest.raises(MediaInvalidArgument):
        assets.capability(
            model_id="bad id",
            catalog_state="LISTED",
            catalog_support_image=True,
            protocol_state="UNVERIFIED",
            evidence_level="OFFLINE_CONTRACT",
        )


def test_restore_rejects_path_traversal_and_counts_skips(tmp_path, sources):
    """P2-4/P2-7: a persisted file name may not escape; skips are counted."""
    from thu_assistant.secure_store import SecureStore, create_key_file, load_provider

    backend = "windows-dpapi" if os.name == "nt" else "file-key"
    key_path = tmp_path / "keys" / "master.key"
    create_key_file(key_path, backend)
    store = SecureStore(load_provider(key_path), tmp_path / "data")
    root = tmp_path / "temp"
    assets = MediaAssets(root, state_store=store)
    record_id = assets.state_record_id(SCOPE)
    base_entry = {
        "asset_id": "media_" + "a" * 24,
        "kind": "IMAGE",
        "mime_type": "image/png",
        "byte_size": 10,
        "sha256": "0" * 64,
        "width": 1,
        "height": 1,
        "pixel_count": 1,
        "created_at": "2026-11-16T12:00:00+00:00",
        "expires_at": None,
        "retention": "EXPLICIT_DELETE",
        "account_scope": SCOPE,
        "purpose": "USER_REVIEW",
        "account_domain": "CAMPUS",
        "service_domain": "LOCAL",
        "task_id": TASK,
        "file_name": "../../escape.png",
        "source": {
            "kind": "USER_SELECTED", "source_evidence_id": EVIDENCE,
            "display_name": "e.png", "synthetic": False,
            "source_sha256": "0" * 64, "source_mime": "image/png",
        },
        "transform": {},
        "state": "ACTIVE",
        "uses": 0,
    }
    store.write_json(
        record_id, {"version": 1, "entries": [base_entry]},
        purpose="MEDIA", account_domain="CAMPUS", service_domain="LOCAL", account=SCOPE,
    )
    assert assets.restore_state(SCOPE) == 0
    assert assets.audit_report()["state_skipped"] == 1


def test_image_sanitising_keeps_pillows_global_limit(assets, sources):
    """P2-5: the module must not relax a process-wide Pillow threshold."""
    from PIL import Image

    before = Image.MAX_IMAGE_PIXELS
    register_image(assets, png_file(sources))
    assert Image.MAX_IMAGE_PIXELS == before


def test_limits_match_the_frozen_contract():
    schema = json.loads((ROOT / "contracts" / "v0.2" / "media-reference.schema.json").read_text("utf-8"))
    published = schema["x-image-limits"]
    limits = MediaLimits()
    assert limits.max_image_bytes == published["max_bytes_per_image"]
    assert limits.max_images_per_request == published["max_images_per_request"]
    assert limits.max_total_image_bytes == published["max_total_bytes_per_request"]
    assert limits.max_pixels_per_image == published["max_pixels_per_image"]
    assert list(limits.image_mime_types) == list(published["mime_types"])


def test_stricter_limits_reject_unknown_keys_and_accept_the_contract_shape():
    schema = json.loads((ROOT / "contracts" / "v0.2" / "media-reference.schema.json").read_text("utf-8"))
    assert MediaLimits().stricter(schema["x-image-limits"]).max_image_bytes == 8388608
    with pytest.raises(ValueError):
        MediaLimits().stricter({"max_bytes_per_images": 1024})


def test_source_evidence_id_rejects_control_characters(assets, sources):
    with pytest.raises(MediaInvalidArgument):
        assets.register_user_file(
            png_file(sources), account_scope=SCOPE, task_id=TASK,
            source_evidence_id="line\nbreak",
        )


def test_display_name_strips_bidi_overrides():
    assert "\u202e" not in sanitize_display_name("photo\u202egnp.exe")
    assert sanitize_display_name("photo\u202egnp.exe").endswith(".exe")


def test_text_that_merely_starts_with_mz_or_bm_is_not_magic():
    assert sniffing.detect(b"MZ is a magazine, issue 3").family != "EXECUTABLE"
    assert sniffing.detect(b"BMW owner manual").family != "IMAGE"
    assert sniffing.detect(synthetic.executable_stub()).family == "EXECUTABLE"


def test_finished_bundles_are_pruned_after_their_ttl(assets, sources, clock):
    reference = register_image(assets, png_file(sources), clock=clock)
    bundle = assets.prepare_model_bundle(
        [reference.media_id], account_scope=SCOPE, task_id=TASK,
        request_id="req-synthetic-1", model_id="synthetic-model-a",
    )
    grant = assets.authorize_model_send(bundle.bundle_id, request_id="req-synthetic-1")
    assets.consume_model_bundle(grant, request_id="req-synthetic-1")
    assert assets.broker.bundle_count() == 1
    clock.advance(seconds=301)
    assets.prepare_model_bundle(
        [reference.media_id], account_scope=SCOPE, task_id=TASK,
        request_id="req-synthetic-2", model_id="synthetic-model-a",
    )
    assert assets.broker.bundle_count() == 1
