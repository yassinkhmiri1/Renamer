# Developer Notes

Copyright © 2026 Mohamed Yassin Khmiri

## Source setup

Renamer targets Python 3.10 or newer. The application uses PySide6 for its desktop interface, Pillow for image validation and capture-date inspection, and pygame for optional audio. Install the runtime dependencies from the project root and launch the application:

```text
python -m pip install -r requirements.txt
python main.py
```

The ready-to-use Windows installer is distributed separately through the GitHub Releases page. Users do not need Python or developer tools when they install the release setup file.

## Project structure

```text
main.py                  Qt startup, application identity, rotating logs
core/audio.py            Optional pygame music and state cues
core/backup_manager.py   Verified snapshots and rollback
core/metadata.py         Lossless JPEG/PNG metadata filtering
core/renamer.py          Naming strategies and collision-safe planning
workers/image_worker.py  Background batches, cancellation, ZIP export
ui/main_window.py        Single-window UI and status presentation
assets/                  State artwork, application icon, optional audio
installer/Renamer.iss    Windows installer and uninstaller definition
requirements.txt         Runtime dependencies for source setup
```

## Application flow

`main.py` configures the Windows Qt font directory when available, creates `QApplication`, sets the application identity, configures rotating logs, installs an uncaught-exception hook, shows `RenamerWindow`, and runs the Qt event loop. Logs go to Qt's application-data location under `RENAMER_logs`; the fallback is `~/RENAMER/RENAMER_logs`. Each log is capped at 2 MB and up to five backups are kept.

`RenamerWindow` in `ui/main_window.py` owns the interface, state preview, recent-event log, and active worker. It connects the worker's progress, status, state, failure, and completion signals before starting it. The worker communicates through Qt signals rather than manipulating widgets directly.

`ImageWorker` in `workers/image_worker.py` scans supported regular, non-symlink image files directly inside the selected folder. In-place processing creates a verified backup before modifying source files and can roll back on cancellation or a batch-level error. ZIP export processes staged copies and leaves the source files untouched.

`core/renamer.py` plans numbered, prefixed, or capture-date filenames and avoids collisions. `core/backup_manager.py` verifies backup copies and restores originals. `core/metadata.py` filters supported metadata from JPEG and PNG streams without decoding and re-encoding image data; WebP, BMP, TIFF, and GIF are validated but left unchanged. `core/audio.py` lazily manages optional music and state cues.

## Windows release installer

`installer/Renamer.iss` defines the Inno Setup wizard for Windows 10 and Windows 11 on standard x64 PCs. It installs per-user, creates desktop and Start menu shortcuts, and registers a standard Windows uninstaller. The compiled setup executable is published through GitHub Releases rather than included in the source tree.

When rebuilding the Windows app bundle, collect pygame explicitly:

```powershell
python -m PyInstaller --clean --noconfirm --noconsole --onedir --name Renamer --icon "assets\icon.ico" --add-data "assets;assets" --collect-all pygame main.py
```

`core/audio.py` imports pygame lazily by module name so audio remains optional when running from source. PyInstaller cannot discover that import automatically. `--collect-all pygame` bundles pygame's Python modules, native SDL/mixer libraries, and data files; `--add-data "assets;assets"` separately bundles `assets/audio/main_theme.mp3` and the state artwork. Compile the Inno Setup installer only after verifying both the pygame package and audio assets are present under `dist\Renamer\_internal`.

## Comments and documentation

Keep comments human-authored, concise, and focused on real constraints or non-obvious decisions. Keep docstrings and these notes aligned with behavior when the application changes.
