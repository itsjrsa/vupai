"""Microphone recorder backed by sox `rec`.

Recording is started by spawning `rec` via subprocess.Popen and stopped by
sending SIGINT (not SIGKILL): SIGINT lets sox flush the WAV header so the
file stays valid.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Minimum size (bytes) for a capture to count as "real audio". A denied-mic
# capture yields an empty/header-only WAV. Shared by permissions.py and
# daemon.py so the doctor and the live path use one threshold.
MIN_WAV_BYTES = 2_000


def _has_flag_pair(parts: list[str], flag: str, value: str) -> bool:
    """True if `flag value` appears adjacent in an argv list (e.g. -c 1)."""
    for i in range(len(parts) - 1):
        if parts[i] == flag and parts[i + 1] == value:
            return True
    return False


def _is_vupai_rec(cmd: str) -> bool:
    """Match vupai's exact `rec` invocation (see Recorder.start).

    The fingerprint is `rec -q -c 1 ... -b 16 <tempdir>/...wav`: quiet, mono,
    16-bit, writing a NamedTemporaryFile WAV. This is specific enough to never
    match a user's own interactive sox/rec session.
    """
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if not parts or Path(parts[0]).name != "rec":
        return False
    if "-q" not in parts:
        return False
    if not _has_flag_pair(parts, "-c", "1") or not _has_flag_pair(parts, "-b", "16"):
        return False
    wav = parts[-1]
    return wav.endswith(".wav") and wav.startswith(tempfile.gettempdir())


def reap_orphan_recordings() -> int:
    """Kill stray `rec` processes left behind by a previous daemon.

    A daemon that dies hard (kill -9, crash, OOM) never gets to SIGINT its
    in-flight `rec` child; macOS then reparents that child to launchd (pid 1)
    where it holds the microphone open indefinitely. A freshly started daemon
    has not spawned any `rec` of its own yet, so any process matching vupai's
    exact signature at that point is necessarily stale. Returns the count
    reaped. Best-effort: failures are logged, never raised.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        logger.exception("orphan reap: listing processes failed")
        return 0
    me = os.getpid()
    victims: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, cmd = line.partition(" ")
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == me:
            continue
        if _is_vupai_rec(cmd):
            victims.append(pid)
    for pid in victims:
        # SIGINT first so sox releases CoreAudio cleanly (the WAV is discarded).
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            continue
        except OSError:
            logger.exception("orphan reap: SIGINT to %d failed", pid)
            continue
    if victims:
        time.sleep(0.5)
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)  # escalate if SIGINT was ignored
            except ProcessLookupError:
                pass
            except OSError:
                logger.exception("orphan reap: SIGKILL to %d failed", pid)
        logger.info("orphan reap: killed %d stray rec process(es)", len(victims))
    return len(victims)


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
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # sox ignored SIGINT (wedged driver / heavy load). Force-kill and
                # reap so we never orphan a `rec` child holding the mic; the wav
                # may be truncated, which the caller's size check already handles.
                proc.kill()
                proc.wait()
        finally:
            self._proc = None
            self._wav_path = None
        return wav_path
