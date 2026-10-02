"""Losslessly remove common metadata from JPEG and PNG images.

Image data is copied as-is: this module never saves decoded pixels through
Pillow, which would recompress JPEGs. Other supported formats are validated
and left byte-for-byte unchanged.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)


class MetadataStripError(Exception):
    """Raised when an image is invalid or its format is not supported."""


@dataclass(frozen=True, slots=True)
class MetadataStripResult:
    """Summary of metadata removed from one image."""

    path: Path
    image_format: str
    removed_items: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """Whether any metadata segments or chunks were removed."""
        return bool(self.removed_items)


_PNG_METADATA_CHUNKS = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME", b"pHYs"}
_PNG_PRESERVED_ANCILLARY_CHUNKS = {
    # These affect color interpretation, rendering, or animated image data.
    b"acTL",
    b"bKGD",
    b"cHRM",
    b"cICP",
    b"cLLi",
    b"fcTL",
    b"fdAT",
    b"gAMA",
    b"iCCP",
    b"mDCv",
    b"sBIT",
    b"sRGB",
    b"tRNS",
}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CHUNK_BUFFER_SIZE = 64 * 1024
_JPEG_STANDALONE_MARKERS = {0x01, *range(0xD0, 0xD9)}


def strip_metadata(image_path: str | Path) -> MetadataStripResult:
    """Remove metadata from JPEG/PNG without recompressing the image.

    EXIF, XMP, IPTC, comments, and textual PNG chunks are removed. PNG
    color/rendering chunks and JPEG ICC profiles are retained so metadata
    removal does not silently change how the image is displayed. Other image
    formats are validated but left unchanged, preserving animation and pixels.
    The original file is replaced only after the complete output is written.

    Raises:
        MetadataStripError: If the file is invalid or is not a supported image.
        OSError: If the image cannot be read or atomically replaced.

    Args:
        image_path: Path to the image to validate and process.

    Returns:
        The resolved path, detected format, and descriptions of removed data.
    """
    path = Path(image_path).resolve(strict=True)
    if not path.is_file():
        raise MetadataStripError(f"Not a regular image file: {path}")

    try:
        with Image.open(path) as image:
            image_format = image.format
            image.verify()
    except (OSError, UnidentifiedImageError) as error:
        raise MetadataStripError(f"Unable to validate image: {path}") from error

    if image_format not in {"JPEG", "PNG", "WEBP", "BMP", "TIFF", "GIF"}:
        raise MetadataStripError(
            f"Unsupported image format {image_format!r}"
        )

    if image_format not in {"JPEG", "PNG"}:
        logger.info(
            "Metadata cleanup is not available for %s; leaving %s unchanged",
            image_format,
            path.name,
        )
        return MetadataStripResult(path, image_format, ())

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)

    # Write to a same-directory temporary file so replacement stays atomic.
    try:
        with os.fdopen(descriptor, "wb") as destination, path.open("rb") as source:
            if image_format == "JPEG":
                removed_items = _strip_jpeg(source, destination)
            else:
                removed_items = _strip_png(source, destination)

        if removed_items:
            os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary_path, path)
        else:
            temporary_path.unlink()

        result = MetadataStripResult(path, image_format, tuple(removed_items))
        logger.info(
            "Metadata removal from %s: %s",
            path.name,
            ", ".join(result.removed_items) or "nothing found",
        )
        return result
    except Exception:
        logger.exception("Metadata removal failed for %s", path)
        temporary_path.unlink(missing_ok=True)
        raise


def _strip_jpeg(source: BinaryIO, destination: BinaryIO) -> list[str]:
    """Copy JPEG segments, omitting metadata and comments.

    Preserved segment bytes and compressed scan data are copied without
    decoding or recompressing the image.
    """
    if source.read(2) != b"\xff\xd8":
        raise MetadataStripError("Invalid JPEG start marker")
    destination.write(b"\xff\xd8")
    removed_items: list[str] = []

    while True:
        marker_start = source.read(1)
        if marker_start != b"\xff":
            raise MetadataStripError("Malformed JPEG marker stream")

        fill_bytes = bytearray()
        marker_byte = source.read(1)
        while marker_byte == b"\xff":
            fill_bytes.extend(marker_byte)
            marker_byte = source.read(1)
        if not marker_byte:
            raise MetadataStripError("Unexpected end of JPEG file")

        marker = marker_byte[0]
        marker_bytes = marker_start + bytes(fill_bytes) + marker_byte
        if marker == 0x00:
            raise MetadataStripError("Unexpected stuffed byte outside JPEG image data")

        if marker in _JPEG_STANDALONE_MARKERS:
            destination.write(marker_bytes)
            continue
        if marker == 0xD9:
            destination.write(marker_bytes)
            shutil.copyfileobj(source, destination)
            break

        length_bytes = source.read(2)
        if len(length_bytes) != 2:
            raise MetadataStripError("Truncated JPEG segment length")
        segment_length = int.from_bytes(length_bytes, "big")
        if segment_length < 2:
            raise MetadataStripError("Invalid JPEG segment length")
        payload = source.read(segment_length - 2)
        if len(payload) != segment_length - 2:
            raise MetadataStripError("Truncated JPEG segment")

        keep_segment = True
        label = f"APP{marker - 0xE0}" if 0xE0 <= marker <= 0xEF else None
        if label is not None:
            # Preserve ICC color profiles and Adobe's CMYK/YCCK interpretation.
            keep_segment = (marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00")) or (
                marker == 0xEE and payload.startswith(b"Adobe")
            )
        elif marker == 0xFE:
            label = "COM"
            keep_segment = False

        if keep_segment:
            destination.write(marker_bytes)
            destination.write(length_bytes)
            destination.write(payload)
        else:
            removed_items.append(label or f"JPEG marker 0x{marker:02X}")

        if marker == 0xDA:
            # Metadata is stored before the first scan in standard JPEG files.
            shutil.copyfileobj(source, destination)
            break

    return removed_items


def _strip_png(source: BinaryIO, destination: BinaryIO) -> list[str]:
    """Copy valid PNG chunks, omit metadata, and validate every chunk CRC.

    Chunk payloads are streamed to avoid buffering large images in memory.
    """
    if source.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
        raise MetadataStripError("Invalid PNG signature")
    destination.write(_PNG_SIGNATURE)
    removed_items: list[str] = []

    while True:
        chunk_header = source.read(8)
        if len(chunk_header) != 8:
            raise MetadataStripError("Truncated PNG chunk header")

        data_length = int.from_bytes(chunk_header[:4], "big")
        chunk_type = chunk_header[4:]
        keep_chunk = _keep_png_chunk(chunk_type)
        chunk_name = chunk_type.decode("ascii", errors="replace")
        if keep_chunk:
            destination.write(chunk_header)

        checksum = zlib.crc32(chunk_type)
        remaining = data_length
        while remaining:
            data = source.read(min(remaining, _PNG_CHUNK_BUFFER_SIZE))
            if not data:
                raise MetadataStripError(f"Truncated PNG {chunk_name} chunk")
            checksum = zlib.crc32(data, checksum)
            if keep_chunk:
                destination.write(data)
            remaining -= len(data)

        stored_checksum = source.read(4)
        if len(stored_checksum) != 4:
            raise MetadataStripError(f"Truncated PNG {chunk_name} checksum")
        if checksum & 0xFFFFFFFF != int.from_bytes(stored_checksum, "big"):
            raise MetadataStripError(f"Invalid PNG {chunk_name} checksum")

        if keep_chunk:
            destination.write(stored_checksum)
        else:
            removed_items.append(chunk_name)

        if chunk_type == b"IEND":
            break

    return removed_items


def _keep_png_chunk(chunk_type: bytes) -> bool:
    """Return whether a PNG chunk is required or needed to render the image.

    Known metadata and malformed chunk names are rejected; ancillary chunks
    are kept only when listed in the rendering-preservation allowlist.
    """
    if chunk_type in _PNG_METADATA_CHUNKS or len(chunk_type) != 4:
        return False
    is_ancillary = bool(chunk_type[0] & 0x20)
    return not is_ancillary or chunk_type in _PNG_PRESERVED_ANCILLARY_CHUNKS