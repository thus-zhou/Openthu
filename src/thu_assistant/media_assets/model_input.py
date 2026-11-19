"""Outbound model-image preparation: preview first, single-use grant second.

Transport is not this module's job and never becomes its job. The broker hands
out in-memory bytes together with the model id and MIME type; it never returns a
path, a URL or an authenticated request. Two rules are structural rather than
advisory:

* a bundle exists before anything can be transferred, and it lists exactly what
  would be sent;
* ``consume`` needs an unexpired grant bound to the same request id and works
  once. A capability probe never resolves a reference, so it cannot read or
  send real image bytes.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from .errors import (
    MediaBundleExpired,
    MediaBundleLimit,
    MediaInvalidArgument,
    MediaReferenceConsumed,
    MediaTransferNotAuthorized,
)
from .limits import MediaLimits
from .registry import MediaReference, ReferenceRegistry, expiry_for, utc_now

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,120}$")
_CLEAN = re.compile(r"[\x00-\x1f\x7f]")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class BundleItem:
    media_id: str
    mime_type: str
    byte_size: int
    width: int | None
    height: int | None
    display_name: str

    def as_preview(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "width": self.width,
            "height": self.height,
            "display_name": self.display_name,
        }


@dataclass
class TransferGrant:
    token: str
    bundle_id: str
    request_id: str
    expires_at: datetime
    consumed: bool = False


@dataclass
class ModelAssetBundle:
    bundle_id: str
    request_id: str
    model_id: str
    account_scope: str
    task_id: str
    items: tuple[BundleItem, ...]
    text_chars: int
    text_preview: str
    text_truncated: bool
    created_at: datetime
    expires_at: datetime
    state: str = "PREPARED"
    grants: list[TransferGrant] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(item.byte_size for item in self.items)

    @property
    def image_count(self) -> int:
        return len(self.items)

    def preview(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "request_id": self.request_id,
            "model_id": self.model_id,
            "image_count": self.image_count,
            "total_image_bytes": self.total_bytes,
            "text": {
                "chars": self.text_chars,
                "preview": self.text_preview,
                "truncated": self.text_truncated,
            },
            "images": [item.as_preview() for item in self.items],
            "state": self.state,
            "expires_at": self.expires_at.isoformat(),
            "note": "不会自动发送；发送需要本次请求的单次授权",
        }


@dataclass(frozen=True)
class OutboundImage:
    media_id: str
    mime_type: str
    data: bytes
    sha256: str


def _preview_text(value: str | None, limit: int) -> tuple[int, str, bool]:
    if value is None:
        return 0, "", False
    if not isinstance(value, str):
        raise MediaInvalidArgument("prompt_text_type")
    cleaned = _CONTROL.sub("", value)
    stripped = cleaned.strip()
    if not stripped:
        return 0, "", False
    character_count = len(stripped)
    if character_count <= limit:
        return character_count, stripped, False
    return character_count, stripped[:limit], True


class ModelInputBroker:
    """Prepares and releases model input under explicit request authorization."""

    def __init__(
        self,
        registry: ReferenceRegistry,
        limits: MediaLimits,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.registry = registry
        self.limits = limits
        self.clock = clock
        self._bundles: dict[str, ModelAssetBundle] = {}

    def prepare(
        self,
        media_ids: Iterable[object],
        *,
        account_scope: str,
        task_id: str,
        request_id: str,
        model_id: str,
        prompt_text: str | None = None,
        now: datetime | None = None,
    ) -> ModelAssetBundle:
        moment = now or self.clock()
        self._prune(moment)
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise MediaInvalidArgument("request_id_format")
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
            raise MediaInvalidArgument("model_id_format")
        if isinstance(media_ids, (str, bytes)):
            raise MediaInvalidArgument("media_ids_not_list")
        try:
            ordered = list(media_ids)
        except TypeError:
            raise MediaInvalidArgument("media_ids_not_list") from None
        if not ordered:
            raise MediaInvalidArgument("empty_media_list")
        if len(set(map(str, ordered))) != len(ordered):
            raise MediaInvalidArgument("duplicate_media_id")
        if len(ordered) > self.limits.max_images_per_request:
            raise MediaBundleLimit("count")

        items: list[BundleItem] = []
        total = 0
        for media_id in ordered:
            reference = self.registry.resolve(
                media_id,
                account_scope=account_scope,
                task_id=task_id,
                purpose="MODEL_INPUT",
                now=moment,
            )
            if reference.kind != "IMAGE":
                raise MediaInvalidArgument("not_an_image")
            if not reference.external_transfer_allowed:
                raise MediaTransferNotAuthorized("reference")
            total += reference.byte_size
            if total > self.limits.max_total_image_bytes:
                raise MediaBundleLimit("total_bytes")
            items.append(
                BundleItem(
                    media_id=reference.media_id,
                    mime_type=reference.mime_type,
                    byte_size=reference.byte_size,
                    width=reference.width,
                    height=reference.height,
                    display_name=reference.display_name,
                )
            )

        text_chars, text_preview, truncated = _preview_text(
            prompt_text, self.limits.preview_chars
        )
        bundle = ModelAssetBundle(
            bundle_id="bundle_" + secrets.token_hex(12),
            request_id=request_id,
            model_id=model_id,
            account_scope=account_scope,
            task_id=task_id,
            items=tuple(items),
            text_chars=text_chars,
            text_preview=text_preview,
            text_truncated=truncated,
            created_at=moment,
            expires_at=expiry_for(moment, self.limits, seconds=self.limits.bundle_ttl_seconds),
        )
        self._bundles[bundle.bundle_id] = bundle
        return bundle

    def get(self, bundle_id: object) -> ModelAssetBundle:
        if not isinstance(bundle_id, str) or bundle_id not in self._bundles:
            raise MediaInvalidArgument("bundle_id_format")
        return self._bundles[bundle_id]

    def authorize(
        self, bundle_id: object, *, request_id: str, now: datetime | None = None
    ) -> TransferGrant:
        moment = now or self.clock()
        bundle = self.get(bundle_id)
        if bundle.request_id != request_id:
            raise MediaTransferNotAuthorized("request")
        if bundle.state != "PREPARED":
            raise MediaTransferNotAuthorized("bundle_state")
        if moment >= bundle.expires_at:
            bundle.state = "EXPIRED"
            raise MediaBundleExpired("authorize")
        grant = TransferGrant(
            token="grant_" + secrets.token_hex(16),
            bundle_id=bundle.bundle_id,
            request_id=request_id,
            expires_at=bundle.expires_at,
        )
        bundle.grants.append(grant)
        bundle.state = "AUTHORIZED"
        return grant

    def consume(
        self,
        grant: TransferGrant,
        *,
        request_id: str,
        reader: Callable[[MediaReference], bytes],
        now: datetime | None = None,
    ) -> tuple[OutboundImage, ...]:
        moment = now or self.clock()
        bundle = self.get(grant.bundle_id)
        if grant.request_id != request_id or bundle.request_id != request_id:
            raise MediaTransferNotAuthorized("request")
        if grant.consumed:
            raise MediaReferenceConsumed("grant")
        if moment >= grant.expires_at:
            raise MediaBundleExpired("consume")
        if bundle.state not in ("AUTHORIZED", "CONSUMED"):
            raise MediaTransferNotAuthorized("bundle_state")

        grant.consumed = True
        images: list[OutboundImage] = []
        for item in bundle.items:
            reference = self.registry.resolve(
                item.media_id,
                account_scope=bundle.account_scope,
                task_id=bundle.task_id,
                purpose="MODEL_INPUT",
                now=moment,
            )
            data = reader(reference)
            images.append(
                OutboundImage(
                    media_id=reference.media_id,
                    mime_type=reference.mime_type,
                    data=data,
                    sha256=reference.sha256,
                )
            )
            self.registry.touch(reference)
        bundle.state = "CONSUMED"
        return tuple(images)

    def cancel(self, bundle_id: object) -> None:
        bundle = self.get(bundle_id)
        if bundle.state in ("PREPARED", "AUTHORIZED"):
            bundle.state = "CANCELLED"

    def _prune(self, now: datetime) -> None:
        """Drop finished bundles past their TTL; they are audit history, not state."""
        expired = [
            bundle_id
            for bundle_id, bundle in self._bundles.items()
            if bundle.state in ("CONSUMED", "CANCELLED")
            and now >= bundle.expires_at
        ]
        for bundle_id in expired:
            self._bundles.pop(bundle_id, None)

    def drop(self, bundle_id: object) -> None:
        self._bundles.pop(str(bundle_id), None)

    def bundle_count(self) -> int:
        return len(self._bundles)

    def active(self) -> list[str]:
        return [
            bundle_id
            for bundle_id, bundle in self._bundles.items()
            if bundle.state in ("PREPARED", "AUTHORIZED")
        ]


def capability_report(
    *,
    model_id: str,
    catalog_state: str,
    catalog_support_image: bool | None,
    protocol_state: str,
    evidence_level: str,
    source_id: str = "model.image_input",
    reason_code: str | None = None,
    warning: str | None = None,
    synthetic: bool = True,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """A conservative ``model-image-capability-v1`` conclusion.

    Catalogue support never becomes AVAILABLE without transport evidence, and
    this function has no access to media references, so a probe cannot read or
    send a real image.
    """
    if catalog_state not in ("LISTED", "NOT_LISTED", "UNKNOWN"):
        raise MediaInvalidArgument("catalog_state")
    if protocol_state not in ("VERIFIED", "UNVERIFIED", "MISMATCH", "UNAVAILABLE"):
        raise MediaInvalidArgument("protocol_state")
    if evidence_level not in (
        "NONE", "OFFLINE_CONTRACT", "SYNTHETIC_TRANSPORT", "LIVE_READ", "UNKNOWN"
    ):
        raise MediaInvalidArgument("evidence_level")
    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
        raise MediaInvalidArgument("model_id_format")
    if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
        raise MediaInvalidArgument("source_id_format")
    if catalog_support_image is not None and not isinstance(catalog_support_image, bool):
        raise MediaInvalidArgument("catalog_support_image_type")
    if not isinstance(synthetic, bool):
        raise MediaInvalidArgument("synthetic_type")
    if checked_at is not None and not isinstance(checked_at, datetime):
        raise MediaInvalidArgument("checked_at_type")
    if reason_code is not None:
        reason_code = _CLEAN.sub("", str(reason_code))[:120] or None
    if warning is not None:
        warning = _CLEAN.sub("", str(warning))[:300] or None
    moment = checked_at or utc_now()

    if catalog_state == "NOT_LISTED":
        state = "UNAVAILABLE"
        protocol_state = "UNAVAILABLE"
        reason_code = reason_code or "MODEL_NOT_LISTED_FOR_IMAGE"
    elif catalog_support_image is False:
        state = "UNAVAILABLE"
        protocol_state = "UNAVAILABLE"
        reason_code = reason_code or "MODEL_WITHOUT_IMAGE_SUPPORT"
    elif (
        catalog_state == "LISTED"
        and catalog_support_image is True
        and protocol_state == "VERIFIED"
        and evidence_level in ("SYNTHETIC_TRANSPORT", "LIVE_READ")
    ):
        state = "AVAILABLE"
        reason_code = reason_code or "IMAGE_PROTOCOL_VERIFIED"
    else:
        state = "UNVERIFIED"
        reason_code = reason_code or "IMAGE_PROTOCOL_NOT_VERIFIED"

    return {
        "model_id": model_id,
        "source_id": source_id,
        "catalog_state": catalog_state,
        "catalog_support_image": catalog_support_image,
        "protocol_state": protocol_state,
        "capability_state": state,
        "reason_code": reason_code,
        "evidence_level": evidence_level,
        "checked_at": moment.isoformat(),
        "source_evidence_id": source_id,
        "warning": warning or "目录标记支持图片不等于当前代理可传输图片",
        "synthetic": synthetic,
    }


__all__ = [
    "BundleItem",
    "ModelAssetBundle",
    "ModelInputBroker",
    "OutboundImage",
    "TransferGrant",
    "capability_report",
]
