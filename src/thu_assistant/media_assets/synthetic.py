"""Pure local synthetic fixtures for media tests and demos.

Nothing here reads a real file, a real account or a network resource. The
fixtures exist so the rejection paths can be exercised deterministically:
disguised extensions, location metadata, a tiny file that claims enormous
dimensions, truncated image data and a valid document of each accepted type.
"""
from __future__ import annotations

import io
import os
import struct
import zlib
from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _pillow():
    from PIL import Image

    return Image


def exif_blob(
    *,
    orientation: int = 1,
    gps: bool = True,
    make: str = "SyntheticCam",
    model: str = "SYNTH-1",
    comment: str = "synthetic metadata",
):
    Image = _pillow()
    exif = Image.Exif()
    if orientation != 1:
        exif[0x0112] = int(orientation)
    exif[0x010F] = make
    exif[0x0110] = model
    exif[0x0132] = "2026:01:01 00:00:00"
    exif[0x010E] = comment
    if gps:
        gps_ifd = exif.get_ifd(0x8825)
        gps_ifd[1] = "N"
        gps_ifd[2] = (39.0, 59.0, 0.0)
        gps_ifd[3] = "E"
        gps_ifd[4] = (116.0, 19.0, 0.0)
    return exif


def _encode(image, fmt: str, *, exif=None, text: str | None = None) -> bytes:
    buffer = io.BytesIO()
    params: dict[str, object] = {}
    if exif is not None:
        params["exif"] = exif.tobytes() if hasattr(exif, "tobytes") else exif
    if fmt == "PNG":
        params["compress_level"] = 6
        if text is not None:
            from PIL import PngImagePlugin

            info = PngImagePlugin.PngInfo()
            info.add_text("Comment", text)
            params["pnginfo"] = info
    elif fmt == "JPEG":
        params["quality"] = 88
        if text is not None:
            params["comment"] = text.encode("utf-8")
    elif fmt == "WEBP":
        params["quality"] = 88
    image.save(buffer, format=fmt, **params)
    return buffer.getvalue()


def png(width: int = 40, height: int = 20, *, color=(30, 90, 180), exif=None,
        text: str | None = None) -> bytes:
    Image = _pillow()
    image = Image.new("RGB", (width, height), color)
    return _encode(image, "PNG", exif=exif, text=text)


def jpeg(width: int = 40, height: int = 20, *, color=(200, 120, 40), exif=None,
         text: str | None = None) -> bytes:
    Image = _pillow()
    image = Image.new("RGB", (width, height), color)
    return _encode(image, "JPEG", exif=exif, text=text)


def webp(width: int = 40, height: int = 20, *, color=(20, 160, 120)) -> bytes:
    Image = _pillow()
    image = Image.new("RGB", (width, height), color)
    return _encode(image, "WEBP")


def rgba_png(width: int = 24, height: int = 16) -> bytes:
    Image = _pillow()
    image = Image.new("RGBA", (width, height), (10, 20, 30, 128))
    return _encode(image, "PNG")


def animated_webp(frames: int = 3) -> bytes:
    Image = _pillow()
    images = [
        Image.new("RGB", (32, 32), (index * 60 % 255, 20, 40)) for index in range(frames)
    ]
    buffer = io.BytesIO()
    images[0].save(
        buffer,
        format="WEBP",
        save_all=True,
        append_images=images[1:],
        duration=80,
        loop=0,
    )
    return buffer.getvalue()


def png_with_gps(*, orientation: int = 1) -> bytes:
    return png(exif=exif_blob(orientation=orientation), text="synthetic location")


def jpeg_with_device_metadata(*, orientation: int = 1) -> bytes:
    return jpeg(exif=exif_blob(orientation=orientation), text="synthetic device note")


def png_header_only(width: int = 20_000, height: int = 20_000) -> bytes:
    """A small, structurally valid PNG whose header claims huge dimensions.

    The IDAT is deliberately not a full image: the point is that the pixel
    limit must reject it before any decode allocation happens.
    """

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00")
    return PNG_MAGIC + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def truncated_jpeg() -> bytes:
    data = jpeg(80, 60)
    return data[: int(len(data) * 0.7)]


def png_magic_padding(size: int) -> bytes:
    """A file that starts like a PNG and is ``size`` bytes long."""
    if size < len(PNG_MAGIC):
        raise ValueError("size is smaller than the PNG signature")
    return PNG_MAGIC + os.urandom(size - len(PNG_MAGIC))


def html_document() -> bytes:
    return (
        b"<!DOCTYPE html><html><head><title>synthetic</title></head>"
        b"<body><p>not an image</p></body></html>"
    )


def svg_document() -> bytes:
    return (
        b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" '
        b'width="10" height="10"><rect width="10" height="10"/></svg>'
    )


def pdf_document() -> bytes:
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )


def zip_document(entry: str = "word/document.xml") -> bytes:
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(entry, "<document/>")
    return buffer.getvalue()


def executable_stub() -> bytes:
    return b"MZ" + b"\x90" * 62 + b"PYTHON-SYNTHETIC-NOT-EXECUTED"


def plain_text() -> bytes:
    return "synthetic course note\nsecond line\n".encode("utf-8")


def json_document() -> bytes:
    return b'{"course": "SYNTHETIC", "term": "2026-2027-1"}'


def write_file(directory: Path, name: str, data: bytes) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


__all__ = [
    "PNG_MAGIC",
    "animated_webp",
    "exif_blob",
    "executable_stub",
    "html_document",
    "jpeg",
    "jpeg_with_device_metadata",
    "json_document",
    "pdf_document",
    "plain_text",
    "png",
    "png_header_only",
    "png_magic_padding",
    "png_with_gps",
    "rgba_png",
    "svg_document",
    "truncated_jpeg",
    "webp",
    "write_file",
    "zip_document",
]
