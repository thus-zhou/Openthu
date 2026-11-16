"""Bounded image decoding and metadata sanitisation.

The pipeline is deliberately conservative:

* the byte limit is checked before a decoder sees the data;
* Pillow's own bomb threshold is disabled locally and replaced by the explicit
  pixel/side limits, so the refusal reason is deterministic and independent of
  Pillow defaults;
* the image is fully decoded and re-encoded. Re-encoding is what removes EXIF,
  GPS, XMP, ICC, comments and any trailing container chunks, including data a
  plain metadata strip would leave behind;
* the derived bytes are re-opened and checked again; a surviving metadata block
  is an internal failure, not a warning. ``jfif*`` keys are structural APP0
  defaults written by the JPEG encoder, not values copied from the source;
* the original file is never modified.

The derived hash, not the original hash, identifies what later modules may send.
"""
from __future__ import annotations

import io
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

from .errors import (
    MediaCancelled,
    MediaDecodeFailed,
    MediaDecoderUnavailable,
    MediaResourceLimit,
    MediaSanitizeIncomplete,
    MediaSizeLimit,
    MediaTypeUnsupported,
)
from .limits import MediaLimits

ORIENTATION_TAG = 0x0112
_ACCEPTED_FORMATS = {"image/jpeg": ("JPEG",), "image/png": ("PNG",), "image/webp": ("WEBP",)}
_JPEG_MODES = {"1", "L", "RGB", "CMYK"}
_METADATA_KEYS = ("exif", "icc_profile", "comment", "xmp", "xml", "photoshop", "dpi")
_PILLOW_LOCK = threading.RLock()


@dataclass(frozen=True)
class ImageResult:
    data: bytes
    mime_type: str
    width: int
    height: int
    pixel_count: int
    orientation_applied: bool
    source_format: str
    transforms: tuple[str, ...]

    @property
    def byte_size(self) -> int:
        return len(self.data)

    def transform_note(self) -> dict[str, object]:
        return {
            "operations": list(self.transforms),
            "orientation_applied": self.orientation_applied,
            "metadata_removed": True,
            "reencoded_as": self.mime_type,
        }


@contextmanager
def _pillow_guard():
    try:
        from PIL import Image
    except ImportError:
        raise MediaDecoderUnavailable("pillow") from None
    with _PILLOW_LOCK:
        previous = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None
        try:
            yield Image
        finally:
            Image.MAX_IMAGE_PIXELS = previous


def _check_cancel(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise MediaCancelled("during_image")


def _open_image(Image, data: bytes, mime_type: str):
    buffer = io.BytesIO(data)
    try:
        image = Image.open(buffer, formats=_ACCEPTED_FORMATS.get(mime_type))
    except Image.UnidentifiedImageError:
        raise MediaDecodeFailed("unidentified") from None
    except Image.DecompressionBombError:
        raise MediaResourceLimit("decode_bomb") from None
    except (OSError, ValueError):
        raise MediaDecodeFailed("header") from None
    if image.format is None or image.format.upper() not in (
        "JPEG", "PNG", "WEBP"
    ):
        image.close()
        raise MediaTypeUnsupported("container")
    return image


def _enforce_limits(image, limits: MediaLimits) -> tuple[int, int]:
    width, height = image.size
    if width <= 0 or height <= 0:
        raise MediaDecodeFailed("dimensions")
    if width > limits.max_side_pixels or height > limits.max_side_pixels:
        raise MediaResourceLimit("side")
    pixels = width * height
    if pixels > limits.max_pixels_per_image:
        raise MediaResourceLimit("pixels")
    return width, height


def _normalise_mode(Image, image, mime_type: str):
    if mime_type == "image/jpeg":
        if image.mode in _JPEG_MODES:
            return image
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[3])
        return background
    if mime_type == "image/png" and image.mode not in ("RGB", "RGBA", "L", "LA"):
        return image.convert("RGBA")
    if mime_type == "image/webp" and image.mode not in ("RGB", "RGBA"):
        return image.convert("RGBA")
    return image


def _encode(Image, image, mime_type: str, *, quality: int = 90) -> bytes:
    output = io.BytesIO()
    image.info = {}
    try:
        if mime_type == "image/jpeg":
            image.save(output, format="JPEG", quality=quality, optimize=False,
                       progressive=False)
        elif mime_type == "image/png":
            image.save(output, format="PNG", compress_level=6)
        else:
            image.save(output, format="WEBP", quality=90, method=4)
    except (OSError, ValueError):
        raise MediaDecodeFailed("encode") from None
    return output.getvalue()


def sanitize(
    data: bytes,
    *,
    mime_type: str,
    limits: MediaLimits,
    cancel: Callable[[], bool] | None = None,
) -> ImageResult:
    """Decode, orient, strip metadata and re-encode one image."""
    if not isinstance(data, bytes) or not data:
        raise MediaDecodeFailed("empty")
    if len(data) > limits.max_image_bytes:
        raise MediaSizeLimit("memory_bytes")
    if mime_type not in limits.image_mime_types:
        raise MediaTypeUnsupported("image_mime")

    _check_cancel(cancel)
    with _pillow_guard() as Image:
        source = _open_image(Image, data, mime_type)
        try:
            width, height = _enforce_limits(source, limits)
            frames = int(getattr(source, "n_frames", 1))
            if frames > 1:
                raise MediaTypeUnsupported("animated")
            try:
                source.verify()
            except (Image.DecompressionBombError, MemoryError):
                raise MediaResourceLimit("verify") from None
            except (OSError, ValueError):
                raise MediaDecodeFailed("verify") from None
        finally:
            source.close()

        _check_cancel(cancel)
        source = _open_image(Image, data, mime_type)
        try:
            try:
                source.load()
            except (Image.DecompressionBombError, MemoryError):
                raise MediaResourceLimit("load") from None
            except (OSError, ValueError):
                raise MediaDecodeFailed("load") from None

            orientation = 1
            try:
                orientation = int(source.getexif().get(ORIENTATION_TAG, 1) or 1)
            except (OSError, ValueError, TypeError):
                orientation = 1

            _check_cancel(cancel)
            try:
                from PIL import ImageOps
                oriented = ImageOps.exif_transpose(source)
            except (OSError, ValueError):
                raise MediaDecodeFailed("orientation") from None
            oriented.info = {}
            oriented = _normalise_mode(Image, oriented, mime_type)
            oriented.info = {}

            derived_width, derived_height = oriented.size
            if (
                derived_width > limits.max_side_pixels
                or derived_height > limits.max_side_pixels
                or derived_width * derived_height > limits.max_pixels_per_image
            ):
                raise MediaResourceLimit("post_orientation")

            encoded = _encode(Image, oriented, mime_type)
            if mime_type == "image/jpeg":
                # A sanitised re-encode may grow; step quality down instead of
                # failing a legitimate photo that already fit the input limit.
                for quality in (80, 70, 60):
                    if len(encoded) <= limits.max_image_bytes:
                        break
                    encoded = _encode(Image, oriented, mime_type, quality=quality)
            oriented.close()
        finally:
            source.close()

        if len(encoded) > limits.max_image_bytes:
            raise MediaSizeLimit("derived_bytes")

        _check_cancel(cancel)
        check = _open_image(Image, encoded, mime_type)
        try:
            if check.size != (derived_width, derived_height):
                raise MediaSanitizeIncomplete("dimensions")
            residue = []
            try:
                if len(check.getexif()):
                    residue.append("exif")
            except (OSError, ValueError):
                residue.append("exif_unreadable")
            for key in _METADATA_KEYS:
                if key in check.info:
                    residue.append(key)
            if residue:
                raise MediaSanitizeIncomplete("residue=" + "-".join(residue))
        finally:
            check.close()

    transforms = ["decoded", "metadata_removed", f"reencoded:{mime_type.split('/')[1]}"]
    if orientation != 1:
        transforms.insert(1, f"orientation:{orientation}")
    return ImageResult(
        data=encoded,
        mime_type=mime_type,
        width=derived_width,
        height=derived_height,
        pixel_count=derived_width * derived_height,
        orientation_applied=orientation != 1,
        source_format=(mime_type.split("/", 1)[1]),
        transforms=tuple(transforms),
    )


def describe_dimensions(data: bytes, *, mime_type: str) -> tuple[int, int]:
    """Header-only dimension read used for diagnostics and tests."""
    with _pillow_guard() as Image:
        image = _open_image(Image, data, mime_type)
        try:
            return image.size
        finally:
            image.close()


__all__ = ["ImageResult", "ORIENTATION_TAG", "describe_dimensions", "sanitize"]
