"""Two-column PySide6 dashboard for RENAMER."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont, QIcon, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core.audio import AudioController
from workers.image_worker import ERROR_ISOLATION_TAG, ImageWorker

logger = logging.getLogger(__name__)

STATE_PRESENTATION: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "ready": (
        "[LAB PROTOCOL] HERE YOU ARE, LAD - LET'S DO SOME SCIENCE",
        ("here you re lad .jpg",),
    ),
    "working": (
        "[WORKER THREAD] OH WAIT, WORKING ON IT... (DON'T TOUCH THE DIAL)",
        ("OH WAIT working on it.jpg",),
    ),
    "caution": (
        "[BACKUP ENGINE] CAUTION: TIME CAPSULE SEALED",
        ("caution.jpg",),
    ),
    "error": (
        "[ERROR ISOLATION] ERRORE: A MUTANT FILE FOUGHT BACK",
        ("errore.jpg", "error.jpg"),
    ),
    "success": (
        "[SUCCESS] LABORATORY UNSCATHED - MANDARK IS CRYING",
        ("Happy for using my tool xD.jpg",),
    ),
}


class _UiLogEmitter(QObject):
    """Bridge Python log records onto the Qt UI thread."""

    message_received = Signal(str)


class _UiLogHandler(logging.Handler):
    """Forward formatted application records to the log panel."""

    def __init__(self, emitter: _UiLogEmitter) -> None:
        """Connect this logging handler to the UI-thread signal emitter."""
        super().__init__(logging.INFO)
        self._emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        """Format a log record and forward it to the read-only log panel."""
        try:
            self._emitter.message_received.emit(self.format(record))
        except Exception:
            self.handleError(record)


class _StateImageLabel(QLabel):
    """Notify the window when the available image area changes size."""

    resized = Signal()

    def resizeEvent(self, event) -> None:
        """Notify the window after the preview label changes size."""
        super().resizeEvent(event)
        self.resized.emit()


class RenamerWindow(QMainWindow):
    """Compact image-renaming window with a live state preview."""

    def __init__(self) -> None:
        """Set up window state, assets, widgets, logging, and startup behavior."""
        super().__init__()
        self._configure_fonts()
        self.setWindowTitle("Renamer - Mohamed Yassin Khmiri")
        self.setMinimumSize(760, 650)
        self.resize(1120, 720)

        self._folder: Path | None = None
        self._worker: ImageWorker | None = None
        self._failed_files: tuple[Path, ...] = ()
        self._close_when_worker_stops = False
        self._root_logger = logging.getLogger()
        self._current_state: str | None = None
        self._pending_pixmap = QPixmap()
        self._fade_phase = "idle"

        self._assets_directory = Path(__file__).resolve().parents[1] / "assets"
        self._audio = AudioController(self._assets_directory / "audio")
        icon_path = self._assets_directory / "icon.ico"
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._build_interface()
        self._log_emitter = _UiLogEmitter(self)
        self._log_emitter.message_received.connect(self.log_view.appendPlainText)
        self._log_handler = _UiLogHandler(self._log_emitter)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
        )
        self._root_logger.addHandler(self._log_handler)

        self._set_visual_state("ready", play_audio=False)
        QTimer.singleShot(0, self.play_startup_audio)

    @staticmethod
    def _configure_fonts() -> None:
        """Point Qt at installed Windows fonts when its bundled font path is missing."""
        system_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        if system_fonts.is_dir():
            os.environ.setdefault("QT_QPA_FONTDIR", str(system_fonts))

    def _build_interface(self) -> None:
        """Apply the visual theme and arrange the single-window dashboard."""
        self.setStyleSheet(
            "QMainWindow, QWidget#appRoot { background: #10191d; color: #e8efeb; }"
            "QLabel { color: #dce7e2; }"
            "QLabel#appTitle { color: #d9e9df; font-size: 11px; font-weight: 700; }"
            "QLabel#eyebrow, QLabel#sectionLabel { color: #9ab4aa; font-size: 9px; font-weight: 800; }"
            "QLabel#imageCaption { color: #a3b6ae; font-size: 10px; }"
            "QLabel#stateBanner { color: #b4e5f7; font-size: 12px; font-weight: 800; }"
            "QToolTip { background: #e5f4eb; color: #15221c; border: 1px solid #7eb992; "
            "border-radius: 4px; padding: 5px 7px; }"
            "QGroupBox { background: #172326; border: 1px solid #334541; "
            "border-radius: 7px; margin-top: 10px; padding: 10px 10px 8px; "
            "font-size: 10px; font-weight: 800; color: #dce9e1; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #a6d9bd; }"
            "QLineEdit { background: #0d1719; border: 1px solid #405650; border-radius: 4px; "
            "padding: 6px 8px; color: #edf3ee; selection-background-color: #367960; }"
            "QLineEdit:focus { border-color: #80c7a0; }"
            "QPushButton { background: #263936; color: #eaf3ed; border: 1px solid #496158; "
            "border-radius: 5px; padding: 6px 9px; font-weight: 700; }"
            "QPushButton:hover { background: #3a5c4f; border-color: #9ad3ad; color: #ffffff; }"
            "QPushButton:pressed { background: #172721; border-color: #68aa82; padding-top: 7px; }"
            "QPushButton:disabled { color: #798985; background: #202b29; border-color: #36433f; }"
            "QPushButton#primaryButton { background: #397f5c; border-color: #7cc596; color: #f7fff8; }"
            "QPushButton#primaryButton:hover { background: #48956d; border-color: #b3ebc3; }"
            "QPushButton#choiceButton { min-height: 30px; padding: 5px 8px; text-align: center; }"
            "QPushButton#choiceButton:checked { background: #285541; border-color: #8bd0a2; color: #f5fff6; }"
            "QPushButton#toggleButton { min-height: 28px; padding: 4px 8px; text-align: center; }"
            "QPushButton#toggleButton:checked { background: #285541; border-color: #8bd0a2; color: #f5fff6; }"
            "QPushButton#muteButton { min-height: 18px; max-height: 18px; padding: 0 4px; }"
            "QProgressBar { background: #0d1719; color: #e3eee7; border: 1px solid #405650; "
            "border-radius: 3px; min-height: 9px; max-height: 9px; text-align: center; }"
            "QProgressBar::chunk { background: #67b888; border-radius: 2px; }"
            "QPlainTextEdit { background: #0d1719; color: #b9cbc3; border: 1px solid #334541; "
            "border-radius: 4px; padding: 4px; selection-background-color: #367960; }"
            "QSlider::groove:horizontal { height: 4px; background: #405650; border-radius: 2px; }"
            "QSlider::sub-page:horizontal { background: #76bd91; border-radius: 2px; }"
            "QSlider::handle:horizontal { width: 12px; margin: -4px 0; border-radius: 6px; "
            "background: #f1fff5; border: 2px solid #53976d; }"
        )

        central = QWidget(self)
        central.setObjectName("appRoot")
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(16, 5, 16, 10)
        root_layout.setSpacing(6)
        root_layout.addLayout(self._build_top_bar())

        dashboard = QHBoxLayout()
        dashboard.setSpacing(12)
        dashboard.addWidget(self._build_image_panel(), 6)
        dashboard.addWidget(self._build_control_panel(), 5)
        root_layout.addLayout(dashboard, 1)
        root_layout.addWidget(self._build_status_panel())
        self.setCentralWidget(central)

    def _build_top_bar(self) -> QHBoxLayout:
        """Create the volume slider and mute control row."""
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 2)
        header.addStretch(1)

        audio = QHBoxLayout()
        audio.setSpacing(7)
        audio_label = QLabel("AUDIO")
        audio_label.setObjectName("eyebrow")
        audio.addWidget(audio_label)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.volume_slider.setFixedWidth(112)
        self.volume_slider.setAccessibleName("Audio volume")
        self.volume_slider.setToolTip("Set the background music volume.")
        self.volume_slider.valueChanged.connect(self._change_volume)
        audio.addWidget(self.volume_slider)
        self.volume_label = QLabel("70%")
        self.volume_label.setMinimumWidth(36)
        self.volume_label.setToolTip("Current background music volume.")
        audio.addWidget(self.volume_label)
        self.mute_button = QPushButton("Mute")
        self.mute_button.setObjectName("muteButton")
        self.mute_button.setCheckable(True)
        self.mute_button.setFixedSize(56, 22)
        self.mute_button.setToolTip("Mute or restore the background music.")
        self.mute_button.toggled.connect(self._audio.set_muted)
        audio.addWidget(self.mute_button)
        header.addLayout(audio)
        return header

    def _build_image_panel(self) -> QGroupBox:
        """Build the state-art preview and its fade transition."""
        panel = QGroupBox("LIVE STATE")
        panel.setToolTip("Shows the picture for the current app state.")
        panel.setMinimumWidth(0)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(9, 13, 9, 8)
        layout.setSpacing(5)

        self.image_frame = QFrame()
        self.image_frame.setObjectName("imageFrame")
        self.image_frame.setStyleSheet(
            "QFrame#imageFrame { background: #0a1117; border: 1px solid #334541; border-radius: 5px; }"
        )
        image_layout = QVBoxLayout(self.image_frame)
        image_layout.setContentsMargins(0, 0, 0, 0)
        self.image_label = _StateImageLabel("Image preview")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setToolTip("The active state picture appears here.")
        self.image_label.setMinimumSize(0, 0)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image_label.setStyleSheet("background: transparent; color: #738895;")
        self.image_label.resized.connect(self._scale_display_pixmap)
        self._image_opacity = QGraphicsOpacityEffect(self.image_label)
        self._image_opacity.setOpacity(1.0)
        self.image_label.setGraphicsEffect(self._image_opacity)
        self._image_fade = QPropertyAnimation(self._image_opacity, b"opacity", self)
        self._image_fade.setDuration(210)
        self._image_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._image_fade.finished.connect(self._finish_image_fade)
        image_layout.addWidget(self.image_label, 1)
        layout.addWidget(self.image_frame, 1)

        caption = QLabel("CURRENT STATE")
        caption.setObjectName("imageCaption")
        caption.setToolTip("This picture changes while the job runs.")
        layout.addWidget(caption)
        return panel

    def _build_control_panel(self) -> QWidget:
        """Create folder, rename, export, safety, and action controls."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        directory_box = QGroupBox("FOLDER")
        directory_box.setToolTip("Pick the folder with the images you want to rename.")
        directory_layout = QHBoxLayout(directory_box)
        directory_layout.setContentsMargins(8, 13, 8, 7)
        directory_layout.setSpacing(6)
        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Choose or enter a folder")
        self.folder_edit.setToolTip("Enter a folder path or choose one with Browse.")
        self.folder_edit.setClearButtonEnabled(True)
        self.folder_edit.editingFinished.connect(self._sync_folder_from_input)
        self.folder_edit.textChanged.connect(self._folder_text_changed)
        path_row.addWidget(self.folder_edit, 1)
        self.folder_button = QPushButton("Choose folder")
        self.folder_button.setFixedWidth(98)
        self.folder_button.setToolTip("Choose the folder that has your images.")
        self.folder_button.clicked.connect(self._choose_folder)
        path_row.addWidget(self.folder_button)
        directory_layout.addLayout(path_row)
        layout.addWidget(directory_box)

        strategy_box = QGroupBox("RENAME STYLE")
        strategy_box.setToolTip("Choose how your images will be named.")
        strategy_layout = QVBoxLayout(strategy_box)
        strategy_layout.setContentsMargins(8, 13, 8, 7)
        strategy_layout.setSpacing(4)
        self.mode_group = QButtonGroup(self)
        self.numbered_mode_button = QPushButton("Numbered")
        self.prefix_mode_button = QPushButton("Prefix")
        self.date_mode_button = QPushButton("Capture date")
        mode_row = QHBoxLayout()
        mode_row.setSpacing(5)
        for button in (
            self.numbered_mode_button,
            self.prefix_mode_button,
            self.date_mode_button,
        ):
            button.setObjectName("choiceButton")
            button.setCheckable(True)
            self.mode_group.addButton(button)
            button.setToolTip(
                {
                    self.numbered_mode_button: "Name files Image_001, Image_002, and so on.",
                    self.prefix_mode_button: "Add your own word before each number.",
                    self.date_mode_button: "Use each picture's capture date when available.",
                }[button]
            )
            mode_row.addWidget(button)
        self.numbered_mode_button.setChecked(True)
        strategy_layout.addLayout(mode_row)
        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("For example, pic1")
        self.prefix_edit.setToolTip("Type the word to put before each image number.")
        self.prefix_edit.setEnabled(False)
        self.prefix_edit.setVisible(False)
        self.prefix_mode_button.toggled.connect(self.prefix_edit.setEnabled)
        self.prefix_mode_button.toggled.connect(self.prefix_edit.setVisible)
        self.prefix_edit.setFixedHeight(31)
        strategy_layout.addWidget(self.prefix_edit)
        layout.addWidget(strategy_box)

        output_box = QGroupBox("SAVE AS")
        output_box.setToolTip("Rename the originals or save renamed copies in a ZIP file.")
        output_layout = QHBoxLayout(output_box)
        output_layout.setContentsMargins(8, 13, 8, 7)
        output_layout.setSpacing(6)
        self.output_group = QButtonGroup(self)
        self.in_place_button = QPushButton("In this folder")
        self.zip_export_button = QPushButton("ZIP file")
        for button in (self.in_place_button, self.zip_export_button):
            button.setObjectName("choiceButton")
            button.setCheckable(True)
            self.output_group.addButton(button)
            output_layout.addWidget(button)
        self.in_place_button.setToolTip("Rename the images in their current folder.")
        self.zip_export_button.setToolTip("Keep originals untouched and save renamed copies to a ZIP file.")
        self.in_place_button.setChecked(True)
        self.zip_export_button.toggled.connect(self._update_output_options)
        layout.addWidget(output_box)

        safety_box = QGroupBox("OPTIONS")
        safety_box.setToolTip("Choose whether to remove image details and keep a backup.")
        safety_layout = QHBoxLayout(safety_box)
        safety_layout.setContentsMargins(8, 13, 8, 7)
        safety_layout.setSpacing(6)
        self.metadata_toggle = QPushButton("Clean image details")
        self.metadata_toggle.setObjectName("toggleButton")
        self.metadata_toggle.setCheckable(True)
        self.metadata_toggle.setChecked(True)
        self.metadata_toggle.setToolTip(
            "Remove extra image details from JPEG and PNG files. Other formats stay unchanged."
        )
        self.backup_toggle = QPushButton("Backup first")
        self.backup_toggle.setObjectName("toggleButton")
        self.backup_toggle.setCheckable(True)
        self.backup_toggle.setChecked(True)
        self.backup_toggle.setToolTip("Save a backup before changing the original files.")
        safety_layout.addWidget(self.metadata_toggle, 1)
        safety_layout.addWidget(self.backup_toggle, 1)
        layout.addWidget(safety_box)

        actions = QHBoxLayout()
        self.process_button = QPushButton("Rename images")
        self.process_button.setObjectName("primaryButton")
        self.process_button.setEnabled(False)
        self.process_button.setToolTip("Rename the images in the chosen folder.")
        self.process_button.clicked.connect(self._start_processing)
        self.retry_button = QPushButton("Try failed again")
        self.retry_button.setEnabled(False)
        self.retry_button.setToolTip("Try renaming the images that failed last time.")
        self.retry_button.clicked.connect(self._retry_failed)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setToolTip("Stop the current job and restore the originals.")
        self.cancel_button.clicked.connect(self._cancel_processing)
        actions.setSpacing(6)
        actions.addWidget(self.process_button, 2)
        actions.addWidget(self.retry_button)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        return panel

    def _build_status_panel(self) -> QGroupBox:
        """Create the state banner, progress bar, and compact event log."""
        panel = QGroupBox("STATUS")
        panel.setToolTip("See what is happening and follow the job's progress.")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 13, 10, 7)
        layout.setSpacing(4)
        self.state_label = QLabel()
        self.state_label.setObjectName("stateBanner")
        self.state_label.setWordWrap(False)
        self.state_label.setMinimumHeight(18)
        self.state_label.setToolTip("Current app state.")
        layout.addWidget(self.state_label)
        self.status_label = QLabel("Ready")
        self.status_label.setWordWrap(False)
        self.status_label.setStyleSheet("color: #aab9b1; font-size: 10px;")
        self.status_label.setToolTip("More detail about the current job.")
        layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setToolTip("How much of the current job is complete.")
        layout.addWidget(self.progress_bar)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Processing details will appear here.")
        self.log_view.setToolTip("A short log of what happened during the last job.")
        self.log_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.log_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.log_view.setMinimumHeight(34)
        self.log_view.setMaximumHeight(38)
        self.log_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.log_view.setFont(QFont("Consolas", 9))
        layout.addWidget(self.log_view)
        return panel

    def _change_volume(self, value: int) -> None:
        """Update the volume label and apply the selected audio level."""
        self.volume_label.setText(f"{value}%")
        self._audio.set_volume(value / 100)

    def _folder_text_changed(self, text: str) -> None:
        """Enable the main action only while the typed path is a real folder."""
        path = Path(text).expanduser() if text.strip() else None
        self._folder = path if path is not None and path.is_dir() else None
        self.process_button.setEnabled(self._folder is not None and not self._is_worker_running())

    def _sync_folder_from_input(self) -> bool:
        """Validate and normalize the folder field before starting a job."""
        text = self.folder_edit.text().strip()
        if not text:
            self._folder = None
            self.process_button.setEnabled(False)
            return False
        folder = Path(text).expanduser()
        if not folder.is_dir():
            self._folder = None
            self.process_button.setEnabled(False)
            self.status_label.setText("That folder does not exist. Choose another one.")
            return False
        self._folder = folder
        self.folder_edit.setText(str(folder))
        self.folder_edit.setCursorPosition(0)
        self.process_button.setEnabled(not self._is_worker_running())
        return True

    def _update_output_options(self) -> None:
        """Apply the safety and action-button settings for the selected output."""
        zip_export = self.zip_export_button.isChecked()
        self.backup_toggle.setEnabled(not zip_export)
        self.metadata_toggle.setEnabled(not zip_export)
        if zip_export:
            self.metadata_toggle.setChecked(True)
        self.process_button.setText("Create ZIP" if zip_export else "Rename images")
        self.process_button.setToolTip(
            "Save renamed copies in a ZIP and leave originals alone."
            if zip_export
            else "Rename the images in the chosen folder."
        )

    def play_startup_audio(self) -> None:
        """Start the optional startup track; safe to call more than once."""
        self._audio.play_startup()

    def _set_visual_state(self, state: str, play_audio: bool = True) -> None:
        """Update the banner, preview image, color, and optional state sound."""
        presentation = STATE_PRESENTATION.get(state)
        if presentation is None:
            logger.warning("Ignoring unknown worker state %r", state)
            return

        self._current_state = state
        banner, image_names = presentation
        self.state_label.setText(banner)
        self.state_label.setProperty("renamerState", state)
        state_colors = {
            "ready": "#83c9e2",
            "working": "#68c5e7",
            "caution": "#d5ad65",
            "error": "#d9847a",
            "success": "#76c99b",
        }
        self.state_label.setStyleSheet(
            f"color: {state_colors[state]}; font-size: 13px; font-weight: 700;"
        )

        image_path = next(
            (
                self._assets_directory / name
                for name in image_names
                if (self._assets_directory / name).is_file()
            ),
            None,
        )
        pixmap = QPixmap(str(image_path)) if image_path else QPixmap()
        if pixmap.isNull():
            self._pending_pixmap = QPixmap()
            self._image_fade.stop()
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(banner)
            self._image_opacity.setOpacity(1.0)
            logger.warning("No usable state image is available for %s", state)
        else:
            self.image_label.setText("")
            self._transition_image(pixmap)

        if play_audio:
            self._audio.play_state_cue(state)

    def _transition_image(self, pixmap: QPixmap) -> None:
        """Fade from the displayed state image to the pending image."""
        if self._image_fade.state() == QPropertyAnimation.State.Running:
            self._image_fade.stop()
        self._pending_pixmap = pixmap
        current = self.image_label.pixmap()
        if current is None or current.isNull():
            self._set_display_pixmap(pixmap)
            self._animate_image_opacity(0.0, 1.0, "in")
        else:
            self._animate_image_opacity(self._image_opacity.opacity(), 0.0, "out")

    def _animate_image_opacity(self, start: float, end: float, phase: str) -> None:
        """Run one half of the image fade and remember its transition phase."""
        self._fade_phase = phase
        self._image_fade.setStartValue(start)
        self._image_fade.setEndValue(end)
        self._image_fade.start()

    def _finish_image_fade(self) -> None:
        """Swap images after fade-out or reset opacity after fade-in."""
        if self._fade_phase == "out":
            self._set_display_pixmap(self._pending_pixmap)
            self._animate_image_opacity(0.0, 1.0, "in")
        else:
            self._fade_phase = "idle"
            self._image_opacity.setOpacity(1.0)

    def _set_display_pixmap(self, pixmap: QPixmap) -> None:
        """Store the original image and scale it to the current preview size."""
        self._display_pixmap = pixmap
        self._scale_display_pixmap()

    def _scale_display_pixmap(self) -> None:
        """Scale the active image smoothly so it fills the preview area."""
        pixmap = getattr(self, "_display_pixmap", QPixmap())
        if pixmap.isNull():
            return
        available = self.image_label.contentsRect().size()
        if available.width() <= 0 or available.height() <= 0:
            return
        self.image_label.setPixmap(
            pixmap.scaled(
                available,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Rescale the preview image after the window itself is resized."""
        super().resizeEvent(event)
        self._scale_display_pixmap()

    def _choose_folder(self) -> None:
        """Open the folder picker and validate the selected directory."""
        selected = QFileDialog.getExistingDirectory(self, "Choose a folder")
        if selected:
            self.folder_edit.setText(selected)
            self._sync_folder_from_input()
            logger.info("Selected image folder %s", self._folder)

    def _start_processing(self) -> None:
        """Start a batch using every supported image in the chosen folder."""
        self._launch_processing()

    def _retry_failed(self) -> None:
        """Retry only the files recorded as failed by the previous batch."""
        if self._failed_files:
            logger.info("Retrying %d failed image(s)", len(self._failed_files))
            self._launch_processing(self._failed_files)

    def _launch_processing(self, files: tuple[Path, ...] | None = None) -> None:
        """Validate options, connect a worker, and start a full or retry batch."""
        if self._is_worker_running() or not self._sync_folder_from_input():
            return
        if files is None:
            self._failed_files = ()
        if self.prefix_mode_button.isChecked() and not self.prefix_edit.text().strip():
            self.status_label.setText("Add a prefix first.")
            self.prefix_edit.setFocus()
            return

        mode = "default"
        if self.prefix_mode_button.isChecked():
            mode = "prefix"
        elif self.date_mode_button.isChecked():
            mode = "date"

        output_archive_path: Path | None = None
        if self.zip_export_button.isChecked():
            archive_name, _ = QFileDialog.getSaveFileName(
                self,
                "Save renamed images as a ZIP",
                str(self._folder / "renamed_images.zip"),
                "ZIP archives (*.zip)",
            )
            if not archive_name:
                return
            output_archive_path = Path(archive_name)
            if output_archive_path.suffix.lower() != ".zip":
                output_archive_path = output_archive_path.with_suffix(".zip")

        self.progress_bar.setValue(0)
        self.log_view.clear()
        self._set_controls_enabled(False)
        self.retry_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._set_visual_state("working" if output_archive_path else "caution")
        self.status_label.setText(
            "Preparing ZIP export copies..."
            if output_archive_path
            else "Making sure your originals are backed up..."
        )

        self._worker = ImageWorker(
            self._folder,
            mode=mode,
            prefix=self.prefix_edit.text(),
            keep_backup=self.backup_toggle.isChecked(),
            strip_metadata_enabled=self.metadata_toggle.isChecked(),
            output_archive_path=output_archive_path,
            parent=self,
            files=files,
        )
        self._worker.progress_changed.connect(self.progress_bar.setValue)
        self._worker.status_changed.connect(self.status_label.setText)
        self._worker.state_changed.connect(self._set_visual_state)
        self._worker.file_failed.connect(self._show_file_failure)
        self._worker.completed.connect(self._processing_finished)
        self._worker.finished.connect(self._worker_thread_finished)
        self._worker.start()

    def _show_file_failure(self, path: str, error: str) -> None:
        """Append one failed image and its error message to the event log."""
        self.log_view.appendPlainText(f"{ERROR_ISOLATION_TAG}: {Path(path).name}: {error}")

    def _cancel_processing(self) -> None:
        """Request safe worker cancellation without force-stopping its thread."""
        if self._is_worker_running():
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling and restoring originals...")
            self._worker.requestInterruption()

    def _set_controls_enabled(self, enabled: bool) -> None:
        """Lock or restore controls while a worker is processing images."""
        for control in (
            self.folder_edit,
            self.folder_button,
            self.numbered_mode_button,
            self.prefix_mode_button,
            self.date_mode_button,
            self.prefix_edit,
            self.in_place_button,
            self.zip_export_button,
            self.metadata_toggle,
            self.backup_toggle,
        ):
            control.setEnabled(enabled)
        self.retry_button.setEnabled(enabled and bool(self._failed_files))
        self.process_button.setEnabled(enabled and self._folder is not None)

    def _processing_finished(self, succeeded: bool, message: str) -> None:
        """Update failure history, progress, state, and the final status text."""
        if self._worker is not None:
            self._failed_files = tuple(self._worker.failed_files)
        self.retry_button.setEnabled(bool(self._failed_files))
        if succeeded:
            self.progress_bar.setValue(100)
            self._set_visual_state("success")
        self.status_label.setText(message)

    def _worker_thread_finished(self) -> None:
        """Release the worker reference, restore controls, and honor close requests."""
        self._worker = None
        self.cancel_button.setEnabled(False)
        self._set_controls_enabled(True)
        self._update_output_options()
        self.process_button.setEnabled(self._folder is not None)
        if self._close_when_worker_stops:
            self.close()

    def _is_worker_running(self) -> bool:
        """Return whether the current worker exists and is still active."""
        return self._worker is not None and self._worker.isRunning()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Cancel active work before closing and shut down the audio controller."""
        if self._is_worker_running():
            self._close_when_worker_stops = True
            self._worker.requestInterruption()
            self.status_label.setText("Cancelling and restoring originals before closing...")
            event.ignore()
            return
        self._root_logger.removeHandler(self._log_handler)
        self._audio.shutdown()
        super().closeEvent(event)