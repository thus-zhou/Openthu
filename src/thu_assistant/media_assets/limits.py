"""Frozen engineering limits for local media handling.

Numbers come from ``contracts/v0.2/media-reference.schema.json``
(``x-image-limits``). They are Openthu engineering limits, not school rules.
A platform contract may be stricter; ``MediaLimits.stricter`` takes the smaller
value instead of letting one module quietly widen the boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES_PER_REQUEST = 4
MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024
MAX_PIXELS_PER_IMAGE = 16_000_000

# Not in the frozen contract: an extra engineering guard so a header claiming a
# 20000x100 pixel image cannot slip past a pure pixel-count rule. Documented in
# README; lowering it never widens the published limits.
MAX_SIDE_PIXELS = 20_000

# The contract caps ``byte_size`` at 134217728 for any kind.
MAX_ATTACHMENT_BYTES = 128 * 1024 * 1024

IMAGE_MIME_TYPES = ("image/jpeg", "image/png", "image/webp")

ATTACHMENT_MIME_TYPES = (
    "application/pdf",
    "application/zip",
    "application/gzip",
    "application/json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "text/markdown",
    "text/plain",
)

_MIME = r"^[a-z0-9][a-z0-9.+-]+/[a-z0-9][a-z0-9.+-]+$"


@dataclass(frozen=True)
class MediaLimits:
    """One immutable limits object shared by every entry point."""

    max_image_bytes: int = MAX_IMAGE_BYTES
    max_images_per_request: int = MAX_IMAGES_PER_REQUEST
    max_total_image_bytes: int = MAX_TOTAL_IMAGE_BYTES
    max_pixels_per_image: int = MAX_PIXELS_PER_IMAGE
    max_side_pixels: int = MAX_SIDE_PIXELS
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES
    reference_ttl_seconds: int = 900
    bundle_ttl_seconds: int = 300
    preview_chars: int = 200
    cleanup_grace_seconds: int = 0
    image_mime_types: tuple[str, ...] = IMAGE_MIME_TYPES
    attachment_mime_types: tuple[str, ...] = ATTACHMENT_MIME_TYPES

    def __post_init__(self) -> None:
        for name in (
            "max_image_bytes",
            "max_images_per_request",
            "max_total_image_bytes",
            "max_pixels_per_image",
            "max_side_pixels",
            "max_attachment_bytes",
            "reference_ttl_seconds",
            "bundle_ttl_seconds",
            "preview_chars",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.cleanup_grace_seconds < 0:
            raise ValueError("cleanup_grace_seconds must not be negative")
        for field in ("image_mime_types", "attachment_mime_types"):
            values = getattr(self, field)
            if not values or any(
                not isinstance(item, str) or not item.count("/") == 1 for item in values
            ):
                raise ValueError(f"{field} must be a non-empty sequence of MIME types")

    def stricter(self, platform: Mapping[str, object]) -> "MediaLimits":
        """Adopt a platform contract only where it is stricter.

        Unknown keys are ignored. A platform MIME list is intersected with the
        local list; an empty intersection is an input error, because a platform
        that accepts nothing here cannot receive local media.
        """
        changes: dict[str, object] = {}
        numeric = {
            "max_bytes_per_image": "max_image_bytes",
            "max_images_per_request": "max_images_per_request",
            "max_total_bytes_per_request": "max_total_image_bytes",
            "max_pixels_per_image": "max_pixels_per_image",
        }
        for key, field in numeric.items():
            value = platform.get(key)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"platform limit {key} must be a positive integer")
            current = getattr(self, field)
            changes[field] = min(current, value)
        mime_types = platform.get("mime_types")
        if mime_types is not None:
            if not isinstance(mime_types, (list, tuple)) or not mime_types:
                raise ValueError("platform mime_types must be a non-empty sequence")
            allowed = tuple(
                item for item in self.image_mime_types if item in set(mime_types)
            )
            if not allowed:
                raise ValueError("platform image MIME list admits no supported type")
            changes["image_mime_types"] = allowed
        return replace(self, **changes) if changes else self


DEFAULT_LIMITS = MediaLimits()

__all__ = [
    "ATTACHMENT_MIME_TYPES",
    "DEFAULT_LIMITS",
    "IMAGE_MIME_TYPES",
    "MAX_ATTACHMENT_BYTES",
    "MAX_IMAGES_PER_REQUEST",
    "MAX_IMAGE_BYTES",
    "MAX_PIXELS_PER_IMAGE",
    "MAX_SIDE_PIXELS",
    "MAX_TOTAL_IMAGE_BYTES",
    "MediaLimits",
]
