"""Optional pygame-backed startup music and state cues."""

from __future__ import annotations

import importlib
import logging
import math
import os
from array import array
from pathlib import Path
from types import ModuleType
from typing import Final

logger = logging.getLogger(__name__)


class AudioController:
    """Safely manage optional startup music, state cues, and volume."""

    _STATE_CUES: Final[dict[str, str]] = {
        "ready": "ready.wav",
        "working": "working.wav",
        "caution": "caution.wav",
        "error": "error.wav",
        "success": "success.wav",
    }
    _STATE_FREQUENCIES: Final[dict[str, int]] = {
        "ready": 660,
        "working": 440,
        "caution": 330,
        "error": 220,
        "success": 880,
    }

    def __init__(self, audio_directory: Path) -> None:
        """Store the asset directory and initialize playback state.

        The pygame mixer is loaded lazily when playback is first requested.
        """
        self._audio_directory = audio_directory
        self._pygame: ModuleType | None = None
        self._mixer_ready = False
        self._startup_attempted = False
        self._volume = 0.7
        self._muted = False

    def _ensure_mixer(self) -> bool:
        """Load pygame and initialize its mixer, returning whether it is ready."""
        if self._mixer_ready:
            return True
        try:
            os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
            if self._pygame is None:
                self._pygame = importlib.import_module("pygame")
            mixer = getattr(self._pygame, "mixer")
            if not mixer.get_init():
                mixer.init(frequency=22_050, size=-16, channels=1)
            self._mixer_ready = True
            self._apply_volume()
            return True
        except Exception:
            logger.warning("Audio is unavailable; continuing without sound", exc_info=True)
            return False

    def play_startup(self) -> None:
        """Play the startup track once, when the main window is shown."""
        if self._startup_attempted:
            return
        self._startup_attempted = True
        startup_track = self._audio_directory / "main_theme.mp3"
        if not startup_track.is_file():
            logger.info("Startup audio not found at %s", startup_track)
            return
        if not self._ensure_mixer():
            return
        try:
            mixer = getattr(self._pygame, "mixer")
            mixer.music.load(str(startup_track))
            mixer.music.set_volume(self._effective_volume())
            mixer.music.play(loops=-1)
            logger.info("Playing startup audio %s", startup_track.name)
        except Exception:
            logger.warning("Unable to play startup audio", exc_info=True)

    def play_state_cue(self, state: str) -> None:
        """Play the WAV cue for ``state`` or synthesize its configured tone.

        Unknown states have no generated fallback tone.
        """
        cue_name = self._STATE_CUES.get(state)
        cue_path = self._audio_directory / cue_name if cue_name else None
        if not self._ensure_mixer():
            return
        try:
            mixer = getattr(self._pygame, "mixer")
            if cue_path is not None and cue_path.is_file():
                sound = mixer.Sound(str(cue_path))
                sound.set_volume(self._effective_volume())
                sound.play()
            else:
                self._play_generated_tone(mixer, state)
        except Exception:
            logger.warning("Unable to play %s audio cue", state, exc_info=True)

    def _play_generated_tone(self, mixer: object, state: str) -> None:
        """Play a short sine-wave cue when ``state`` has a configured frequency.

        Generated audio is skipped unless the mixer uses signed 16-bit samples.
        """
        frequency = self._STATE_FREQUENCIES.get(state)
        if frequency is None:
            return
        sample_rate, sample_format, channels = getattr(mixer, "get_init")()
        if sample_format != -16:
            logger.debug("Skipping generated audio cue for unsupported mixer format %s", sample_format)
            return

        sample_count = int(sample_rate * 0.12)
        samples = array(
            "h",
            (
                int(7000 * math.sin(2 * math.pi * frequency * index / sample_rate))
                for index in range(sample_count)
            ),
        )
        if channels > 1:
            samples = array("h", (sample for value in samples for sample in (value,) * channels))
        sound = getattr(mixer, "Sound")(buffer=samples.tobytes())
        sound.set_volume(self._effective_volume())
        sound.play()

    def set_volume(self, volume: float) -> None:
        """Clamp and store playback volume in the inclusive range zero to one."""
        self._volume = max(0.0, min(1.0, volume))
        self._apply_volume()

    def set_muted(self, muted: bool) -> None:
        """Mute or restore the configured playback volume."""
        self._muted = muted
        self._apply_volume()

    def _effective_volume(self) -> float:
        """Return the chosen volume, or zero when audio is muted."""
        return 0.0 if self._muted else self._volume

    def _apply_volume(self) -> None:
        """Send the effective volume to pygame when the mixer is available."""
        if self._mixer_ready and self._pygame is not None:
            try:
                getattr(self._pygame, "mixer").music.set_volume(self._effective_volume())
            except Exception:
                logger.debug("Unable to update audio volume", exc_info=True)

    def shutdown(self) -> None:
        """Release the mixer without making shutdown failure fatal."""
        if not self._mixer_ready or self._pygame is None:
            return
        try:
            getattr(self._pygame, "mixer").quit()
        except Exception:
            logger.debug("Audio mixer shutdown failed", exc_info=True)
        self._mixer_ready = False
