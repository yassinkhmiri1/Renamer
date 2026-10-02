"""Cancellable background transactions for image batches."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Literal, Sequence

from PySide6.QtCore import QThread, Signal

from core.backup_manager import BackupManager, BackupSnapshot
from core.metadata import strip_metadata
from core.renamer import RenameMode, plan_renames

logger = logging.getLogger(__name__)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif"})
WorkerState = Literal["ready", "working", "caution", "error", "success"]
ERROR_ISOLATION_TAG = "[ERROR ISOLATION] ERRORE: A MUTANT FILE FOUGHT BACK"


class BatchCancelled(Exception):
    """Raised internally when the user requests a safe batch cancellation."""


class ImageWorker(QThread):
    """Process a folder while keeping the UI thread responsive.

    Each image is handled independently. Failed images remain untouched and
    later images continue; cancelling or a batch-level failure restores every
    input from the verified pre-processing backup.
    """

    progress_changed = Signal(int)
    status_changed = Signal(str)
    state_changed = Signal(str)
    file_failed = Signal(str, str)
    completed = Signal(bool, str)

    def __init__(
        self,
        folder: str | Path,
        mode: RenameMode = "default",
        prefix: str = "",
        keep_backup: bool = True,
        strip_metadata_enabled: bool = True,
        output_archive_path: str | Path | None = None,
        parent=None,
        files: Sequence[Path] | None = None,
    ) -> None:
        """Store batch options and initialize the worker's result state."""
        super().__init__(parent)
        self.folder = Path(folder)
        self.mode = mode
        self.prefix = prefix
        self.keep_backup = keep_backup
        self.output_archive_path = Path(output_archive_path) if output_archive_path else None
        self.strip_metadata_enabled = strip_metadata_enabled or self.output_archive_path is not None
        self._requested_files = tuple(Path(path) for path in files) if files is not None else None
        self.failed_files: list[Path] = []
        self.result_success = False
        self.result_message = ""

    def run(self) -> None:
        """Run the in-place batch or hand ZIP requests to the export path."""
        if self.output_archive_path is not None:
            self._run_zip_export()
            return

        manager = BackupManager()
        snapshot: BackupSnapshot | None = None
        changed_paths: list[Path] = []

        try:
            candidates = (
                self._requested_files
                if self._requested_files is not None
                else tuple(self.folder.iterdir())
            )
            files = sorted(
                (
                    path
                    for path in candidates
                    if path.parent.resolve() == self.folder.resolve()
                    and path.is_file()
                    and not path.is_symlink()
                    and path.suffix.lower() in IMAGE_EXTENSIONS
                ),
                key=lambda path: path.name.casefold(),
            )
            if not files:
                raise ValueError("No supported images were found in that folder")

            self._set_state("caution", "Making sure your original files are backed up...")
            snapshot = manager.create_backup(files, keep_archive=self.keep_backup)
            if snapshot.archive_path is not None:
                logger.info("Verified original backup retained at %s", snapshot.archive_path)
            else:
                logger.info("Verified temporary rollback copy is ready")

            operations = plan_renames(
                files,
                self.mode,
                self.prefix,
                protect_source_names=True,
            )
            changed_paths = [operation.source for operation in operations]
            self._set_state("working", "Backup is ready. Renaming your images...")

            failures: list[tuple[Path, str]] = []
            succeeded = 0
            total = len(operations)
            for index, operation in enumerate(operations, start=1):
                if self.isInterruptionRequested():
                    raise BatchCancelled("Cancellation requested")

                work_path: Path | None = None
                destination_created = False
                try:
                    descriptor, work_name = tempfile.mkstemp(
                        prefix=".RENAMER-work-",
                        suffix=operation.source.suffix,
                        dir=operation.source.parent,
                    )
                    os.close(descriptor)
                    work_path = Path(work_name)
                    changed_paths.append(work_path)
                    self.status_changed.emit(f"Renaming {operation.source.name}...")
                    shutil.copy2(operation.source, work_path)
                    if self.strip_metadata_enabled:
                        strip_metadata(work_path)

                    if operation.destination == operation.source:
                        os.replace(work_path, operation.destination)
                    else:
                        self._publish_without_overwrite(work_path, operation.destination)
                        destination_created = True
                        changed_paths.append(operation.destination)
                        try:
                            operation.source.unlink()
                        except OSError:
                            operation.destination.unlink(missing_ok=True)
                            destination_created = False
                            changed_paths.remove(operation.destination)
                            raise

                    succeeded += 1
                    logger.info(
                        "Processed image %s as %s",
                        operation.source.name,
                        operation.destination.name,
                    )
                except Exception as error:
                    if work_path is not None:
                        work_path.unlink(missing_ok=True)
                        changed_paths.remove(work_path)
                    if destination_created:
                        operation.destination.unlink(missing_ok=True)
                        if operation.destination in changed_paths:
                            changed_paths.remove(operation.destination)
                    failures.append((operation.source, str(error)))
                    self.failed_files.append(operation.source)
                    self.file_failed.emit(str(operation.source), str(error))
                    self._set_state(
                        "error",
                        f"{ERROR_ISOLATION_TAG}\nSkipped {operation.source.name}; continuing batch.",
                    )
                    logger.exception("Skipped image %s after processing error", operation.source)
                    self._set_state("working", f"Moving on to {operation.source.name}...")

                self.progress_changed.emit(round(index * 100 / total))

            if self.isInterruptionRequested():
                raise BatchCancelled("Cancellation requested")

            if failures:
                message = (
                    f"Completed {succeeded} of {total} images; "
                    f"{len(failures)} failed. See the log for details."
                )
            else:
                message = f"Processed {succeeded} image(s) successfully"

            manager.discard_temporary_backup(snapshot)
            final_state: WorkerState = "error" if failures else "success"
            self._finish(not failures, message, final_state)
        except Exception as error:
            cancelled = isinstance(error, BatchCancelled)
            if cancelled:
                self._set_state("caution", "Stopping and restoring your original files...")
                logger.info("Batch cancelled; restoring original files")
            else:
                logger.exception("Batch processing aborted")

            if snapshot is not None:
                try:
                    manager.rollback(snapshot, changed_paths)
                except Exception:
                    logger.exception("Unable to restore the full batch from backup")
                    error = RuntimeError(f"{error}; rollback failed; see log for details")

            message = "Cancelled; original files restored" if cancelled else f"Batch failed: {error}"
            self._finish(False, message, "error")

    def _run_zip_export(self) -> None:
        """Create a processed ZIP from staged copies, leaving source files untouched."""
        archive_path = self.output_archive_path
        temporary_archive: Path | None = None
        try:
            if archive_path is None:
                raise ValueError("ZIP export requires an output archive path")
            if not archive_path.parent.is_dir():
                raise ValueError("The ZIP destination folder does not exist")

            candidates = (
                self._requested_files
                if self._requested_files is not None
                else tuple(self.folder.iterdir())
            )
            files = sorted(
                (
                    path
                    for path in candidates
                    if path.parent.resolve() == self.folder.resolve()
                    and path.is_file()
                    and not path.is_symlink()
                    and path.suffix.lower() in IMAGE_EXTENSIONS
                ),
                key=lambda path: path.name.casefold(),
            )
            if not files:
                raise ValueError("No supported images were found in that folder")

            self._set_state("working", "Getting your renamed pictures ready for the ZIP...")
            with tempfile.TemporaryDirectory(prefix="RENAMER_export_") as staging_name:
                staging = Path(staging_name)
                staged_sources: list[Path] = []
                for source in files:
                    if self.isInterruptionRequested():
                        raise BatchCancelled("Cancellation requested")
                    staged = staging / source.name
                    shutil.copy2(source, staged)
                    staged_sources.append(staged)

                operations = plan_renames(
                    staged_sources,
                    self.mode,
                    self.prefix,
                    protect_source_names=True,
                )
                total = len(operations)
                for index, operation in enumerate(operations, start=1):
                    if self.isInterruptionRequested():
                        raise BatchCancelled("Cancellation requested")
                    work_path: Path | None = None
                    try:
                        descriptor, work_name = tempfile.mkstemp(
                            prefix=".RENAMER-work-",
                            suffix=operation.source.suffix,
                            dir=staging,
                        )
                        os.close(descriptor)
                        work_path = Path(work_name)
                        shutil.copy2(operation.source, work_path)
                        strip_metadata(work_path)
                        if operation.destination == operation.source:
                            os.replace(work_path, operation.destination)
                        else:
                            self._publish_without_overwrite(work_path, operation.destination)
                            operation.source.unlink()
                        self.status_changed.emit(f"Adding {operation.destination.name} to the ZIP...")
                    except Exception as error:
                        if work_path is not None:
                            work_path.unlink(missing_ok=True)
                        self.failed_files.append(self.folder / operation.source.name)
                        self.file_failed.emit(str(self.folder / operation.source.name), str(error))
                        raise RuntimeError(f"Unable to process {operation.source.name}: {error}") from error
                    self.progress_changed.emit(round(index * 90 / total))

                if self.isInterruptionRequested():
                    raise BatchCancelled("Cancellation requested")

                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{archive_path.stem}.", suffix=".tmp", dir=archive_path.parent
                )
                os.close(descriptor)
                temporary_archive = Path(temporary_name)
                with zipfile.ZipFile(
                    temporary_archive,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    for operation in operations:
                        archive.write(
                            operation.destination,
                            arcname=operation.destination.name,
                        )
                with zipfile.ZipFile(temporary_archive, "r") as archive:
                    corrupt_member = archive.testzip()
                    if corrupt_member is not None:
                        raise OSError(f"ZIP verification failed for {corrupt_member}")
                os.replace(temporary_archive, archive_path)
                temporary_archive = None

            self.progress_changed.emit(100)
            self._finish(
                True,
                f"Exported {len(files)} processed image(s) to {archive_path}",
                "success",
            )
        except Exception as error:
            if temporary_archive is not None:
                temporary_archive.unlink(missing_ok=True)
            cancelled = isinstance(error, BatchCancelled)
            if cancelled:
                message = "ZIP export cancelled; original files were not changed"
                self._finish(False, message, "caution")
            else:
                logger.exception("ZIP export failed")
                self._finish(False, f"ZIP export failed: {error}", "error")

    @staticmethod
    def _publish_without_overwrite(work_path: Path, destination: Path) -> None:
        """Reserve a new destination exclusively, then atomically publish it."""
        descriptor = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        os.close(descriptor)
        try:
            os.replace(work_path, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def _set_state(self, state: WorkerState, message: str) -> None:
        """Send a state key and its human-readable status to the UI."""
        self.state_changed.emit(state)
        self.status_changed.emit(message)

    def _finish(self, succeeded: bool, message: str, state: WorkerState) -> None:
        """Store the final result and notify connected UI slots."""
        self.result_success = succeeded
        self.result_message = message
        self._set_state(state, message)
        self.completed.emit(succeeded, message)