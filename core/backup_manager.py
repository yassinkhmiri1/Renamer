"""Create restorable ZIP or temporary shadow backups for image batches."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BackupSnapshot:
    """Verified pre-processing copy and original paths required for rollback."""

    files: tuple[Path, ...]
    archive_path: Path | None = None
    shadow_directory: Path | None = None
    file_stats: tuple[tuple[int, int, int], ...] = ()


class BackupManager:
    """Manage pre-processing copies and restore original image files."""

    def create_backup(self, files: list[Path], keep_archive: bool = True) -> BackupSnapshot:
        """Create and verify a byte-exact copy before the caller mutates files.

        The original filenames are retained in the archive or shadow directory.
        A snapshot is returned only after every stored file matches its original
        SHA-256 digest, so callers can safely begin renaming or metadata removal.

        Args:
            files: Original files to include in the snapshot.
            keep_archive: Store a persistent ZIP archive when true; otherwise
                use a temporary shadow directory.

        Returns:
            A verified snapshot that can restore each original file.

        Raises:
            ValueError: If files is empty, contains duplicates, or contains a
                non-regular file.
            OSError: If copying or verifying the snapshot fails.
        """
        normalized_files = tuple(Path(file).resolve(strict=True) for file in files)
        if not normalized_files:
            raise ValueError("Cannot create a backup for an empty file list")
        if len(set(normalized_files)) != len(normalized_files):
            raise ValueError("Cannot back up the same file more than once")
        if any(not file.is_file() for file in normalized_files):
            raise ValueError("Backups can only contain regular files")
        root = Path(os.path.commonpath([str(file.parent) for file in normalized_files]))
        file_stats = tuple(
            (file.stat().st_mode, file.stat().st_atime_ns, file.stat().st_mtime_ns)
            for file in normalized_files
        )
        original_digests = tuple(_file_digest(file) for file in normalized_files)

        if keep_archive:
            backup_directory = root / ".RENAMER_backups"
            backup_directory.mkdir(exist_ok=True)
            os.chmod(backup_directory, 0o700)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f"RENAMER_backup_{stamp}_", suffix=".zip", dir=backup_directory
            )
            os.close(descriptor)
            archive_path = Path(temporary_name)
            try:
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                    for file in normalized_files:
                        archive.write(file, file.relative_to(root).as_posix())

                with zipfile.ZipFile(archive_path, "r") as archive:
                    expected_names = [file.relative_to(root).as_posix() for file in normalized_files]
                    if archive.namelist() != expected_names:
                        raise OSError("Backup verification failed: file list does not match originals")
                    corrupt_file = archive.testzip()
                    if corrupt_file is not None:
                        raise OSError(f"Backup verification failed: corrupt archive member {corrupt_file}")
                    for file, member, expected_digest in zip(
                        normalized_files, expected_names, original_digests
                    ):
                        with archive.open(member, "r") as stored_file:
                            if _stream_digest(stored_file) != expected_digest:
                                raise OSError(f"Backup verification failed for {file.name}")
            except Exception:
                logger.exception("Failed to create or verify backup archive %s", archive_path)
                archive_path.unlink(missing_ok=True)
                raise

            snapshot = BackupSnapshot(
                normalized_files,
                archive_path=archive_path,
                file_stats=file_stats,
            )
            logger.info("Created verified backup archive %s for %d file(s)", archive_path, len(files))
            return snapshot

        shadow_directory = Path(tempfile.mkdtemp(prefix="RENAMER_backup_"))
        try:
            for file, expected_digest in zip(normalized_files, original_digests):
                shadow_file = shadow_directory / file.relative_to(root)
                shadow_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, shadow_file)
                if _file_digest(shadow_file) != expected_digest:
                    raise OSError(f"Backup verification failed for {file.name}")
        except Exception:
            logger.exception("Failed to create or verify temporary backup")
            shutil.rmtree(shadow_directory, ignore_errors=True)
            raise
        snapshot = BackupSnapshot(
            normalized_files,
            shadow_directory=shadow_directory,
            file_stats=file_stats,
        )
        logger.info("Created verified temporary backup for %d file(s)", len(files))
        return snapshot

    def rollback(self, snapshot: BackupSnapshot, changed_paths: list[Path] | None = None) -> None:
        """Restore original files after removing generated paths.

        Args:
            snapshot: Verified backup created before processing.
            changed_paths: Generated or changed files to remove before restore.

        Raises:
            ValueError: If the snapshot contains no restorable storage.
            OSError: If a file cannot be removed or restored.
        """
        logger.warning("Rolling back %d original file(s)", len(snapshot.files))
        for path in changed_paths or []:
            candidate = Path(path)
            if candidate.exists() and candidate.is_file():
                candidate.unlink()

        root = Path(os.path.commonpath([str(file.parent) for file in snapshot.files]))
        if snapshot.archive_path is not None:
            with zipfile.ZipFile(snapshot.archive_path, "r") as archive:
                for original, saved_file_attributes in zip(snapshot.files, snapshot.file_stats):
                    self._restore_from_bytes(
                        original,
                        archive.read(original.relative_to(root).as_posix()),
                        saved_file_attributes,
                    )
        elif snapshot.shadow_directory is not None:
            for original in snapshot.files:
                shutil.copy2(snapshot.shadow_directory / original.relative_to(root), original)
            shutil.rmtree(snapshot.shadow_directory, ignore_errors=True)
        else:
            raise ValueError("Backup snapshot has no restorable storage")
        logger.info("Rollback restored %d original file(s)", len(snapshot.files))

    def discard_temporary_backup(self, snapshot: BackupSnapshot) -> None:
        """Remove shadow-copy storage after success; retain archive backups."""
        if snapshot.shadow_directory is not None:
            shutil.rmtree(snapshot.shadow_directory, ignore_errors=True)

    @staticmethod
    def _restore_from_bytes(
        path: Path,
        data: bytes,
        saved_file_attributes: tuple[int, int, int],
    ) -> None:
        """Write original bytes and restore file mode and access/modification times."""
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(data)
            os.replace(temporary_name, path)
            os.chmod(path, saved_file_attributes[0])
            os.utime(path, ns=(saved_file_attributes[1], saved_file_attributes[2]))
        except Exception:
            logger.exception("Failed to restore backup for %s", path)
            Path(temporary_name).unlink(missing_ok=True)
            raise


def _file_digest(path: Path) -> bytes:
    """Calculate a file's SHA-256 digest without reading it all at once."""
    with path.open("rb") as image_file:
        return _stream_digest(image_file)


def _stream_digest(image_file: BinaryIO) -> bytes:
    """Hash a binary stream a chunk at a time and return its SHA-256 digest."""
    digest = sha256()
    for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.digest()