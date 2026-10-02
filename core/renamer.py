"""Filename generation and collision-safe planning for image batches."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

RenameMode = Literal["default", "prefix", "date"]


@dataclass(frozen=True, slots=True)
class RenameOperation:
    """One source-to-destination rename operation."""

    source: Path
    destination: Path


def plan_renames(
    files: list[Path],
    mode: RenameMode = "default",
    prefix: str = "",
    protect_source_names: bool = False,
) -> list[RenameOperation]:
    """Build deterministic, collision-free rename destinations.

    Files are numbered from one, using enough digits for the whole batch.
    Existing files outside the batch are never overwritten.

    Args:
        files: Image paths to include in the plan.
        mode: Naming strategy to apply to every path.
        prefix: Name prefix used when ``mode`` is ``"prefix"``.
        protect_source_names: Whether to avoid reusing another source name.

    Returns:
        Rename operations in case-insensitive source-name order.

    Raises:
        ValueError: If ``mode`` or a required prefix is invalid.
    """
    ordered_files = sorted((Path(file) for file in files), key=lambda item: item.name.casefold())
    if mode not in {"default", "prefix", "date"}:
        raise ValueError(f"Unsupported rename mode: {mode}")
    if mode == "prefix":
        prefix = _validate_prefix(prefix)

    sources = {file.resolve() for file in ordered_files}
    reserved: set[Path] = set()
    width = max(3, len(str(len(ordered_files))))
    operations: list[RenameOperation] = []

    for index, source in enumerate(ordered_files, start=1):
        source_resolved = source.resolve()
        stem = _base_stem(source, mode, prefix, index, width)
        candidate = source.with_name(f"{stem}{source.suffix.lower()}")
        suffix = 1
        while candidate.resolve() in reserved or (
            candidate.exists()
            and (
                candidate.resolve() not in sources
                or (protect_source_names and candidate.resolve() != source_resolved)
            )
        ):
            candidate = source.with_name(f"{stem}_{suffix}{source.suffix.lower()}")
            suffix += 1
        reserved.add(candidate.resolve())
        operations.append(RenameOperation(source, candidate))

    logger.debug("Planned %d rename operation(s) using %s mode", len(operations), mode)
    return operations


def _base_stem(source: Path, mode: RenameMode, prefix: str, index: int, width: int) -> str:
    """Build one output name's stem from its selected naming style."""
    if mode == "default":
        return f"Image_{index:0{width}d}"
    if mode == "prefix":
        return f"{prefix}_{index:0{width}d}"
    timestamp = _image_timestamp(source)
    return f"{timestamp}_{index:0{width}d}"


def _validate_prefix(prefix: str) -> str:
    """Trim a custom prefix and reject blank or filename-unsafe characters."""
    normalized = prefix.strip()
    if not normalized:
        raise ValueError("Enter a prefix before choosing custom-prefix mode")
    if any(character in normalized for character in '<>:"/\\|?*'):
        raise ValueError("Prefix cannot contain path separators or reserved filename characters")
    return normalized


def _image_timestamp(image_path: Path) -> str:
    """Get an image's capture time, falling back to file timestamps.

    EXIF capture, original, and digitized times are preferred. If none can be
    read, use ``st_birthtime`` when available, then Windows ``st_ctime`` or
    ``st_mtime`` on other platforms.
    """
    try:
        with Image.open(image_path) as image:
            exif_metadata = image.getexif()
            capture_date_value = (
                exif_metadata.get(36867)
                or exif_metadata.get(306)
                or exif_metadata.get(36868)
            )
        if capture_date_value:
            capture_datetime = datetime.strptime(
                str(capture_date_value).strip(), "%Y:%m:%d %H:%M:%S"
            )
            return capture_datetime.strftime("%Y-%m-%d_%H-%M-%S")
    except (OSError, ValueError, UnidentifiedImageError):
        pass

    file_details = image_path.stat()
    creation_time = getattr(file_details, "st_birthtime", None)
    if creation_time is not None:
        image_timestamp = creation_time
    elif os.name == "nt":
        image_timestamp = file_details.st_ctime
    else:
        image_timestamp = file_details.st_mtime
    return datetime.fromtimestamp(image_timestamp).strftime("%Y-%m-%d_%H-%M-%S")