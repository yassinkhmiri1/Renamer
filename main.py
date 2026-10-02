"""Application entry point for RENAMER."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication

from ui.main_window import RenamerWindow

logger = logging.getLogger("RENAMER")


def _configure_logging() -> Path:
    """Configure rotating local logs and return the active log file path.

    Logs are stored in the Qt application-data directory, or in ``~/RENAMER``
    when Qt does not provide a writable location.
    """
    data_directory = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    )
    log_root = Path(data_directory) if data_directory else Path.home() / "RENAMER"
    log_directory = log_root / "RENAMER_logs"
    log_directory.mkdir(parents=True, exist_ok=True)

    log_path = log_directory / "RENAMER.log"
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(threadName)s: %(message)s"
        )
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    return log_path


def _handle_uncaught_exception(
    exception_type: type[BaseException],
    exception: BaseException,
    traceback: TracebackType | None,
) -> None:
    """Log uncaught exceptions while preserving Python's KeyboardInterrupt behavior.

    Args:
        exception_type: The class of the uncaught exception.
        exception: The uncaught exception instance.
        traceback: The traceback associated with the exception.
    """
    if issubclass(exception_type, KeyboardInterrupt):
        sys.__excepthook__(exception_type, exception, traceback)
        return
    logger.critical(
        "Uncaught exception",
        exc_info=(exception_type, exception, traceback),
    )


def main() -> int:
    """Configure the application, show its main window, and run the Qt event loop.

    Returns:
        The exit code returned by Qt's event loop.
    """
    system_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if system_fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(system_fonts))
    application = QApplication(sys.argv)
    application.setOrganizationName("Mohamed Yassin Khmiri")
    application.setApplicationName("Renamer")
    application.setApplicationDisplayName("Renamer - Mohamed Yassin Khmiri")
    log_path = _configure_logging()
    sys.excepthook = _handle_uncaught_exception
    logger.info("RENAMER starting; log file: %s", log_path)

    window = RenamerWindow()
    window.show()
    window.play_startup_audio()
    exit_code = application.exec()
    logger.info("RENAMER exiting with status %d", exit_code)
    logging.shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())