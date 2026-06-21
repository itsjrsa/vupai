"""Microphone recorder backed by sox `rec`.

Recording is started by spawning `rec` via subprocess.Popen and stopped by
sending SIGINT (not SIGKILL): SIGINT lets sox flush the WAV header so the
file stays valid.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from pathlib import Path

# Minimum size (bytes) for a capture to count as "real audio". A denied-mic
# capture yields an empty/header-only WAV. Shared by permissions.py and
# daemon.py so the doctor and the live path use one threshold.
MIN_WAV_BYTES = 2_000


class Recorder:
    def __init__(self, sample_rate: int = 16000, device: str = "") -> None:
        self._sample_rate = sample_rate
        # CoreAudio device name passed to sox via AUDIODEV; "" = system default.
        # Resolved once by the caller (see audio.resolve_device) - never here.
        self._device = device
        self._proc: subprocess.Popen | None = None
        self._wav_path: Path | None = None

    @property
    def is_recording(self) -> bool:
        return self._proc is not None

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("recorder is already recording")
        # delete=False so the file survives after the handle is closed;
        # the daemon owns cleanup of the returned Path.
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        self._wav_path = Path(tmp.name)
        argv = [
            "rec",
            "-q",
            "-c",
            "1",
            "-r",
            str(self._sample_rate),
            "-b",
            "16",
            str(self._wav_path),
        ]
        # Pick a non-default input by setting AUDIODEV in sox's environment;
        # sox honours it for the auto-detected (coreaudio) driver.
        env = None
        if self._device:
            env = {**os.environ, "AUDIODEV": self._device}
        # Silence sox's own stdout/stderr (e.g. the harmless "can't set sample
        # rate 16000; using 24000" device warning) so it never leaks into the
        # doctor output or the daemon pane.
        self._proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    def stop(self) -> Path:
        if self._proc is None or self._wav_path is None:
            raise RuntimeError("recorder is not recording")
        proc = self._proc
        wav_path = self._wav_path
        # SIGINT lets sox flush the WAV header (SIGKILL would corrupt it).
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        finally:
            self._proc = None
            self._wav_path = None
        return wav_path
