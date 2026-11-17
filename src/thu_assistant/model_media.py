"""Model image input on the 3.3 media boundary, plus a traceable source map.

Module 4.5 owns the model-facing half of media handling. It decides whether a
model may receive images at all, builds one request from exactly the selection
the caller named, and keeps the source map traceable afterwards. Three rules are
structural rather than advisory:

* a catalogue flag never enables transport. Only a recorded protocol profile
  whose evidence level is ``SYNTHETIC_TRANSPORT`` or ``LIVE_READ`` does, and the
  shipped build registers none, so every image call fails closed;
* no image bytes leave the process unless the 3.3 broker released them for this
  request through a single-use grant;
* the transport receives one hashed staging file per selected image, reading an
  exact byte count from an exact name. A neighbouring file in the source
  directory is never opened.

Staging exists because the gateway speaks newline-delimited JSON with a 2 MiB
frame guard: an 8 MiB image cannot travel inline without truncation. The staging
root is a private directory supplied by the host; files are created 0600, removed
by the Node transport after a verified read and again, defensively, by this
module when the call returns.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import models
from .bridge import CampusError
from .media_assets import capability_report
from .media_assets.errors import MediaError
from .media_assets.registry import validate_scope, validate_task

SHAPES = ("OPENAI_CONTENT_PARTS_IMAGE_URL",)
SENDING_EVIDENCE = ("SYNTHETIC_TRANSPORT", "LIVE_READ")
EVIDENCE_LEVELS = (
    "NONE",
    "OFFLINE_CONTRACT",
    "SYNTHETIC_TRANSPORT",
    "LIVE_READ",
    "UNKNOWN",
)
IMAGE_MIME_TYPES = ("image/jpeg", "image/png", "image/webp")

# Frozen engineering ceilings from contracts/v0.2/README.md and 1.1. A protocol
# profile may be stricter; it can never widen them.
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 16_000_000

SOURCE_ID = "model.image_input"

_PROTOCOL_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,120}$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,120}$")
_MEDIA_ID = re.compile(r"^media_[A-Za-z0-9_-]{8,64}$")
_CITE = re.compile(r"\[\[([A-Za-z0-9_-]{1,64})\]\]")


# -- protocol evidence --------------------------------------------------


@dataclass(frozen=True)
class ProtocolProfile:
    """Recorded proof that the authenticated proxy carries image parts.

    A profile is configuration, not user input: it is constructed from verified
    upstream evidence, never from a catalogue flag. ``evidence_level`` must be a
    sending level, because a profile whose evidence never demonstrated transport
    would otherwise be indistinguishable from a guess.
    """

    protocol_id: str
    model_id: str
    shape: str = "OPENAI_CONTENT_PARTS_IMAGE_URL"
    media_types: tuple[str, ...] = IMAGE_MIME_TYPES
    max_images: int = MAX_IMAGES
    max_image_bytes: int = MAX_IMAGE_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_pixels: int = MAX_PIXELS
    evidence_level: str = "SYNTHETIC_TRANSPORT"
    source_id: str = SOURCE_ID
    synthetic: bool = True
    checked_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.protocol_id, str) or not _PROTOCOL_ID.fullmatch(
            self.protocol_id
        ):
            raise ValueError("protocol_id must be a lower-case identifier")
        if not isinstance(self.model_id, str) or not re.fullmatch(
            models.MODEL_ID, self.model_id
        ):
            raise ValueError("model_id does not match the catalogue pattern")
        if self.shape not in SHAPES:
            raise ValueError(f"unsupported image message shape {self.shape!r}")
        if self.evidence_level not in SENDING_EVIDENCE:
            raise ValueError(
                "a protocol profile needs SYNTHETIC_TRANSPORT or LIVE_READ evidence"
            )
        if not isinstance(self.source_id, str) or not _SOURCE_ID.fullmatch(
            self.source_id
        ):
            raise ValueError("source_id does not match the evidence pattern")
        if not isinstance(self.synthetic, bool):
            raise ValueError("synthetic must be a boolean")
        if self.checked_at is not None and not isinstance(self.checked_at, str):
            raise ValueError("checked_at must be an ISO string or None")
        media_types = tuple(self.media_types)
        if not media_types or any(item not in IMAGE_MIME_TYPES for item in media_types):
            raise ValueError("media_types must be a non-empty subset of JPEG/PNG/WebP")
        object.__setattr__(self, "media_types", media_types)
        for name, ceiling in (
            ("max_images", MAX_IMAGES),
            ("max_image_bytes", MAX_IMAGE_BYTES),
            ("max_total_bytes", MAX_TOTAL_BYTES),
            ("max_pixels", MAX_PIXELS),
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            if value > ceiling:
                raise ValueError(f"{name} cannot exceed the frozen engineering limit")

    def as_public(self) -> dict[str, Any]:
        """Descriptor for previews and capability rows; no secret material."""
        return {
            "protocol_id": self.protocol_id,
            "model_id": self.model_id,
            "shape": self.shape,
            "media_types": list(self.media_types),
            "max_images": self.max_images,
            "max_image_bytes": self.max_image_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_pixels": self.max_pixels,
            "evidence_level": self.evidence_level,
            "source_id": self.source_id,
            "synthetic": self.synthetic,
            "checked_at": self.checked_at,
        }


class ProtocolRegistry:
    """Explicit, small and empty by default.

    There is no implicit registration and no environment switch that could turn
    an unofficial guess into transport evidence.
    """

    def __init__(self, profiles: Iterable[ProtocolProfile] = ()) -> None:
        self._by_model: dict[str, ProtocolProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: ProtocolProfile) -> None:
        if not isinstance(profile, ProtocolProfile):
            raise TypeError("profile must be a ProtocolProfile")
        if profile.model_id in self._by_model:
            raise ValueError(f"duplicate protocol profile for {profile.model_id}")
        self._by_model[profile.model_id] = profile

    def resolve(self, model_id: object) -> ProtocolProfile | None:
        if not isinstance(model_id, str):
            return None
        return self._by_model.get(model_id)

    def models(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_model))

    def __len__(self) -> int:
        return len(self._by_model)


def capabilities(
    directory: Mapping[str, Any],
    *,
    protocols: ProtocolRegistry | None,
    checked_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """One ``model-image-capability-v1`` row per catalogue model.

    A catalogue flag with no profile stays ``UNVERIFIED``; a model listed
    without image support stays ``UNAVAILABLE``. Only a listed model with a
    verified profile reaches ``AVAILABLE``.
    """
    rows = _directory_rows(directory)
    registry = protocols or ProtocolRegistry()
    report: list[dict[str, Any]] = []
    for row in rows:
        profile = registry.resolve(row["id"])
        report.append(
            capability_report(
                model_id=row["id"],
                catalog_state="LISTED",
                catalog_support_image=row.get("supportImage"),
                protocol_state="VERIFIED" if profile else "UNVERIFIED",
                evidence_level=profile.evidence_level if profile else "OFFLINE_CONTRACT",
                source_id=profile.source_id if profile else SOURCE_ID,
                checked_at=checked_at,
            )
        )
    return report


def _directory_rows(directory: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(directory, Mapping):
        raise CampusError("MODEL_CATALOG_INVALID")
    rows = directory.get("models")
    if not isinstance(rows, list) or not rows:
        raise CampusError("MODEL_CATALOG_INVALID")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise CampusError("MODEL_CATALOG_INVALID")
        model_id = row.get("id")
        if (
            not isinstance(model_id, str)
            or not re.fullmatch(models.MODEL_ID, model_id)
            or model_id in seen
        ):
            raise CampusError("MODEL_CATALOG_INVALID")
        support = row.get("supportImage")
        if support is not None and not isinstance(support, bool):
            raise CampusError("MODEL_CATALOG_INVALID")
        seen.add(model_id)
        out.append({"id": model_id, "supportImage": support})
    return out


# -- selection and staging ----------------------------------------------


@dataclass(frozen=True)
class MediaBinding:
    """The account, task authorization and staging root for one media call.

    The host builds this from the authenticated session; it is never part of a
    tool schema, so a model cannot choose another account or another folder.
    """

    assets: Any
    account_scope: str
    task_id: str
    transport_root: str | os.PathLike[str]
    request_id: str | None = None

    def __post_init__(self) -> None:
        validate_scope(self.account_scope, name="account_scope")
        validate_task(self.task_id)
        for name in (
            "manifest",
            "prepare_model_bundle",
            "authorize_model_send",
            "consume_model_bundle",
            "cancel_task",
        ):
            if not callable(getattr(self.assets, name, None)):
                raise ValueError(f"media pipeline lacks {name}")
        root = Path(os.path.expanduser(str(self.transport_root)))
        if not root.is_absolute():
            raise CampusError("MODEL_IMAGE_TRANSPORT_UNAVAILABLE")


class TransportStage:
    """Private staging files, one per selected image, removed after the call."""

    def __init__(self, root: str | os.PathLike[str], *, sweep_after: float = 900.0) -> None:
        self.root = _transport_root(root)
        self._files: list[Path] = []
        self.sweep(sweep_after)

    def write(self, images: Sequence[Any]) -> list[dict[str, Any]]:
        staged: list[dict[str, Any]] = []
        for image in images:
            data = bytes(image.data)
            if len(data) <= 0:
                raise CampusError("MODEL_IMAGE_STAGING_FAILED")
            token = secrets.token_hex(16)
            path = self.root / f"{token}.bin"
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0)
            )
            try:
                descriptor = os.open(path, flags, 0o600)
            except OSError:
                raise CampusError("MODEL_IMAGE_STAGING_FAILED") from None
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("short write")
                    remaining = remaining[written:]
            except OSError:
                try:
                    os.close(descriptor)
                finally:
                    _unlink(path)
                raise CampusError("MODEL_IMAGE_STAGING_FAILED") from None
            os.close(descriptor)
            self._files.append(path)
            staged.append(
                {
                    "token": token,
                    "media_id": image.media_id,
                    "mime_type": image.mime_type,
                    "sha256": image.sha256,
                    "byte_size": len(data),
                }
            )
        return staged

    def sweep(self, max_age_seconds: float = 900.0) -> int:
        """Remove leftovers from a crashed process; active files are young."""
        removed = 0
        deadline = max(0.0, float(max_age_seconds))
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return 0
        now = time.time()
        for path in entries:
            if not re.fullmatch(r"[a-f0-9]{32}\.bin", path.name):
                continue
            try:
                if now - path.stat().st_mtime < deadline:
                    continue
                path.unlink()
            except OSError:
                continue
            removed += 1
        return removed

    def cleanup(self) -> int:
        removed = 0
        for path in self._files:
            if _unlink(path):
                removed += 1
        self._files.clear()
        return removed

    def __enter__(self) -> "TransportStage":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        # A Windows handle may still be closing; the 0700 root and the next
        # sweep keep the leftover bounded, and the caller still sees the error
        # that made cleanup interesting.
        return False
    return True


def _transport_root(value: object) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise CampusError("MODEL_IMAGE_TRANSPORT_UNAVAILABLE")
    text = str(value)
    if not text or "\x00" in text:
        raise CampusError("MODEL_IMAGE_TRANSPORT_UNAVAILABLE")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        raise CampusError("MODEL_IMAGE_TRANSPORT_UNAVAILABLE")
    try:
        if path.is_symlink():
            raise CampusError("MODEL_IMAGE_TRANSPORT_UNAVAILABLE")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir():
            raise CampusError("MODEL_IMAGE_TRANSPORT_UNAVAILABLE")
        if os.name == "posix":
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise CampusError("MODEL_IMAGE_TRANSPORT_UNAVAILABLE")
    except CampusError:
        raise
    except OSError:
        raise CampusError("MODEL_IMAGE_TRANSPORT_UNAVAILABLE") from None
    return path


# -- request preparation ------------------------------------------------


def _media_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise CampusError("MODEL_IMAGE_SELECTION_INVALID")
    ids: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            # A path, URL or byte field here is a caller error, never ignored.
            if set(item) != {"media_id"}:
                raise CampusError("MODEL_IMAGE_SELECTION_INVALID")
            media_id = item.get("media_id")
        else:
            media_id = item
        if not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id):
            raise CampusError("MODEL_IMAGE_SELECTION_INVALID")
        if media_id in ids:
            raise CampusError("MODEL_IMAGE_SELECTION_INVALID")
        ids.append(media_id)
    if not ids:
        raise CampusError("MODEL_IMAGE_SELECTION_INVALID")
    if len(ids) > MAX_IMAGES:
        raise CampusError("MODEL_IMAGE_LIMIT_EXCEEDED")
    return tuple(ids)


def _protocol_for(protocols: ProtocolRegistry | None, model_id: str) -> ProtocolProfile:
    profile = protocols.resolve(model_id) if protocols is not None else None
    if profile is None:
        raise CampusError("MODEL_IMAGE_PROTOCOL_UNVERIFIED")
    return profile


def _check_selection(
    manifest: Mapping[str, Any], protocol: ProtocolProfile
) -> None:
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise CampusError("MODEL_IMAGE_SELECTION_INVALID")
    if len(assets) > protocol.max_images:
        raise CampusError("MODEL_IMAGE_LIMIT_EXCEEDED")
    total = 0
    for entry in assets:
        if entry.get("mime_type") not in protocol.media_types:
            raise CampusError("MODEL_IMAGE_UNSUPPORTED")
        size = entry.get("byte_size")
        if not isinstance(size, int) or size <= 0 or size > protocol.max_image_bytes:
            raise CampusError("MODEL_IMAGE_LIMIT_EXCEEDED")
        pixels = entry.get("pixel_count") or 0
        if not isinstance(pixels, int) or pixels > protocol.max_pixels:
            raise CampusError("MODEL_IMAGE_LIMIT_EXCEEDED")
        total += size
    if total > protocol.max_total_bytes:
        raise CampusError("MODEL_IMAGE_LIMIT_EXCEEDED")


class ImageRequest:
    """A prepared image request: inspect the preview, then send exactly once."""

    def __init__(
        self,
        *,
        prepared: Mapping[str, Any],
        sources: Sequence[Mapping[str, Any]],
        manifest: Mapping[str, Any],
        bundle: Any,
        protocol: ProtocolProfile,
        binding: MediaBinding,
        request_id: str,
    ) -> None:
        self.prepared = dict(prepared)
        self.sources = list(sources)
        self.manifest = dict(manifest)
        self.bundle = bundle
        self.protocol = protocol
        self.binding = binding
        self.request_id = request_id

    def preview(self) -> dict[str, Any]:
        """Exactly what would be sent, with no bytes and no local path."""
        entries = self.manifest["assets"]
        return {
            "model_id": self.protocol.model_id,
            "protocol": self.protocol.as_public(),
            "request_id": self.request_id,
            "account_scope": self.binding.account_scope,
            "task_id": self.binding.task_id,
            "bundle_id": getattr(self.bundle, "bundle_id", None),
            "text": dict(self.bundle.preview()["text"]),
            "image_count": len(entries),
            "total_image_bytes": sum(int(entry["byte_size"]) for entry in entries),
            "images": [
                {
                    "media_id": entry["asset_id"],
                    "mime_type": entry["mime_type"],
                    "byte_size": entry["byte_size"],
                    "width": entry.get("width"),
                    "height": entry.get("height"),
                    "display_name": entry.get("source", {}).get("display_name"),
                    "synthetic": entry.get("source", {}).get("synthetic"),
                }
                for entry in entries
            ],
            "state": "PREPARED",
            "note": "不会自动发送；发送需要本次请求的单次授权",
        }

    def cancel(self) -> None:
        self.binding.assets.cancel_task(self.binding.task_id)

    def send(self, bridge: Any) -> dict[str, Any]:
        stage = TransportStage(self.binding.transport_root)
        revoked: bool | None = None
        try:
            images = self._consume()
            staged = stage.write(images)
            command = dict(self.prepared)
            command["image_batch"] = {
                "protocol_id": self.protocol.protocol_id,
                "model_id": self.protocol.model_id,
                "shape": self.protocol.shape,
                "evidence_level": self.protocol.evidence_level,
                "max_images": self.protocol.max_images,
                "max_image_bytes": self.protocol.max_image_bytes,
                "max_total_bytes": self.protocol.max_total_bytes,
                "staging_root": str(stage.root),
                "images": staged,
            }
            result = bridge("model", **command)
            return attach_images(
                result,
                self.sources,
                self.manifest["assets"],
                self.prepared,
                self.protocol,
                request_id=self.request_id,
            )
        except CampusError as error:
            evidence = dict(getattr(error, "model_evidence", None) or {})
            if error.code == "CANCELLED":
                revoked = self._revoke()
            evidence = attach_images(
                evidence,
                self.sources,
                self.manifest["assets"],
                self.prepared,
                self.protocol,
                request_id=self.request_id,
                complete=False,
            )
            evidence["media_revoked"] = revoked
            error.model_evidence = evidence
            raise
        finally:
            stage.cleanup()

    def _consume(self) -> tuple[Any, ...]:
        try:
            grant = self.binding.assets.authorize_model_send(
                self.bundle.bundle_id, request_id=self.request_id
            )
            images = self.binding.assets.consume_model_bundle(
                grant, request_id=self.request_id
            )
        except MediaError as error:
            raise CampusError(error.code) from None
        entries = self.manifest["assets"]
        if len(images) != len(entries):
            raise CampusError("MODEL_IMAGE_STAGING_FAILED")
        for image, entry in zip(images, entries):
            data = bytes(image.data)
            if (
                image.media_id != entry["asset_id"]
                or image.mime_type != entry["mime_type"]
                or len(data) != entry["byte_size"]
                or hashlib.sha256(data).hexdigest() != entry["sha256"]
            ):
                raise CampusError("MODEL_IMAGE_STAGING_FAILED")
        return tuple(images)

    def _revoke(self) -> bool:
        try:
            self.binding.assets.cancel_task(self.binding.task_id)
        except MediaError:
            return False
        return True


def prepare(
    args: Mapping[str, Any],
    *,
    media: MediaBinding | None,
    protocols: ProtocolRegistry | None,
) -> ImageRequest:
    """Validate everything before a single byte or request leaves the process."""
    if media is None:
        raise CampusError("MODEL_IMAGE_INPUT_UNAVAILABLE")
    if not isinstance(args, Mapping):
        raise CampusError("INVALID_MODEL_ARGUMENTS")
    model_id = args.get("model")
    if not isinstance(model_id, str):
        raise CampusError("MODEL_REQUIRED_FOR_IMAGE_INPUT")
    protocol = _protocol_for(protocols, model_id)
    media_ids = _media_ids(args.get("images"))

    text_args = {key: value for key, value in args.items() if key != "images"}
    prepared, sources = models.prepare(text_args)
    last_user = next(
        message["content"] for message in reversed(prepared["messages"])
        if message["role"] == "user"
    )
    request_id = media.request_id or ("req_" + secrets.token_hex(12))
    manifest = media.assets.manifest(
        media_ids,
        account_scope=media.account_scope,
        task_id=media.task_id,
        purpose="MODEL_INPUT",
    )
    _check_selection(manifest, protocol)
    bundle = media.assets.prepare_model_bundle(
        media_ids,
        account_scope=media.account_scope,
        task_id=media.task_id,
        request_id=request_id,
        model_id=model_id,
        prompt_text=last_user,
    )
    return ImageRequest(
        prepared=prepared,
        sources=sources,
        manifest=manifest,
        bundle=bundle,
        protocol=protocol,
        binding=media,
        request_id=request_id,
    )


def chat(
    bridge: Any,
    args: Mapping[str, Any],
    *,
    media: MediaBinding | None = None,
    protocols: ProtocolRegistry | None = None,
) -> dict[str, Any]:
    """Image counterpart of ``models.chat``; text calls never reach this path."""
    try:
        request = prepare(args, media=media, protocols=protocols)
        return request.send(bridge)
    except MediaError as error:
        # The media taxonomy keeps its frozen MEDIA_* code; the tool layer gets
        # one error channel and never a free-form detail string.
        raise CampusError(error.code) from None


# -- result mapping -----------------------------------------------------


def _input_digest(prepared: Mapping[str, Any]) -> dict[str, Any]:
    user_text = "".join(
        str(message["content"])
        for message in prepared["messages"]
        if message["role"] == "user" and isinstance(message["content"], str)
    )
    return {
        "kind": "USER_INPUT",
        "chars": len(user_text),
        "sha256": hashlib.sha256(user_text.encode("utf-8")).hexdigest(),
        "messages": len(prepared["messages"]),
    }


def _media_source(entry: Mapping[str, Any]) -> dict[str, Any]:
    source = entry.get("source") or {}
    transform = entry.get("transform") or {}
    operations = list(transform.get("operations") or [])
    source_sha256 = source.get("source_sha256")
    chain = [
        {
            "operation": str(operation)[:64],
            "from_sha256": source_sha256,
            "to_sha256": entry["sha256"],
        }
        for operation in operations
    ]
    chain.append(
        {
            "operation": "sha256_verified_at_send",
            "from_sha256": entry["sha256"],
            "to_sha256": entry["sha256"],
        }
    )
    return {
        "source_id": entry["asset_id"],
        "kind": "DERIVED_IMAGE",
        "mime_type": entry["mime_type"],
        "byte_size": entry["byte_size"],
        "sha256": entry["sha256"],
        "source_sha256": source_sha256,
        "source_mime": source.get("source_mime"),
        "source_kind": source.get("kind"),
        "source_evidence_id": source.get("source_evidence_id"),
        "display_name": source.get("display_name"),
        "synthetic": source.get("synthetic"),
        "expires_at": entry.get("expires_at"),
        "transform": {
            "operations": operations,
            "orientation_applied": transform.get("orientation_applied"),
            "metadata_removed": transform.get("metadata_removed"),
            "reencoded_as": transform.get("reencoded_as"),
        },
        "conversion_chain": chain,
        "independently_verified": False,
        "claim_verified": False,
    }


def attach_images(
    result: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    prepared: Mapping[str, Any],
    protocol: ProtocolProfile,
    *,
    request_id: str,
    complete: bool = True,
) -> dict[str, Any]:
    """Extend ``models.attach`` with image provenance and a merged source map."""
    out = models.attach(dict(result), sources)
    media_sources = [_media_source(entry) for entry in entries]
    registered = {source["source_id"] for source in out.get("sources", [])}
    registered.update(source["source_id"] for source in media_sources)
    identifiers = list(
        dict.fromkeys(_CITE.findall(str(out.get("content", ""))))
    )
    out["citations"] = [
        {
            "source_id": identifier,
            "registered": identifier in registered,
            "claim_verified": False,
        }
        for identifier in identifiers
    ]
    warnings = [
        warning
        for warning in out.get("warnings", [])
        if warning != "UNREGISTERED_MODEL_CITATION"
    ]
    if any(identifier not in registered for identifier in identifiers):
        warnings.append("UNREGISTERED_MODEL_CITATION")
    out["warnings"] = warnings
    out["media_sources"] = media_sources
    out["source_map"] = [
        {"source_id": source["source_id"], "kind": "ORIGINAL_QUOTE",
         "sha256": source.get("sha256")}
        for source in out.get("sources", [])
    ] + [
        {"source_id": source["source_id"], "kind": source["kind"],
         "sha256": source["sha256"], "source_sha256": source["source_sha256"]}
        for source in media_sources
    ]
    out["input"] = _input_digest(prepared)
    out["output_kind"] = "TEXT_SUMMARY"
    out["image_input"] = {
        "count": len(media_sources),
        "total_bytes": sum(int(entry["byte_size"]) for entry in entries),
        "media_ids": [entry["asset_id"] for entry in entries],
        "sha256": [entry["sha256"] for entry in entries],
        "protocol_id": protocol.protocol_id,
        "evidence_level": protocol.evidence_level,
        "request_id": request_id,
        "complete": bool(complete),
    }
    out["citation_check"] = "SOURCE_IDS_ONLY_NOT_FACT_CHECKED"
    out["output_is_authorization"] = False
    return out


__all__ = [
    "EVIDENCE_LEVELS",
    "ImageRequest",
    "IMAGE_MIME_TYPES",
    "MediaBinding",
    "ProtocolProfile",
    "ProtocolRegistry",
    "TransportStage",
    "attach_images",
    "capabilities",
    "chat",
    "prepare",
]
