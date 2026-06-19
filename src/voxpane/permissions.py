"""Best-effort macOS permission probing.

NOTE: macOS TCC (the privacy database) is NOT readable from Python. We cannot
ask the OS "do we hold the Microphone permission?". So every check here is an
*indirect behavioral probe*: we try to do the thing and infer the permission
from success/failure. Results can have false negatives (e.g. a genuinely silent
room) — we always print the exact System Settings panes so the user can verify
and grant manually.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .recorder import MIN_WAV_BYTES, Recorder

# External binaries voxpane needs at runtime. `rec` is provided by sox; a missing
# `rec` is the usual reason the mic probe fails (it can't even spawn the
# recorder), so doctor checks this before blaming the Microphone permission.
REQUIRED_TOOLS: dict[str, str] = {"rec": "sox", "tmux": "tmux"}


def missing_tools() -> list[str]:
    """brew package names for required CLIs that are not on PATH."""
    return [pkg for binary, pkg in REQUIRED_TOOLS.items() if shutil.which(binary) is None]


@dataclass(frozen=True)
class PermissionStatus:
    microphone: bool
    input_monitoring: bool
    accessibility: bool


# The mic probe must actually let sox capture audio before stopping. With no
# wait, the recorder is SIGINT'd before it records anything and writes a
# header-only WAV, which then reads as "no mic" even when permission IS granted.
_PROBE_SECONDS = 0.5


def _probe_microphone(recorder_factory: Callable[[], Recorder]) -> bool:
    """Record a short clip and assert the wav is non-trivial in size.

    Start the recorder, let it capture for ``_PROBE_SECONDS`` (so sox writes real
    samples), then stop and size-check the file. If sox cannot access the mic the
    recorder raises or the file stays header-only. Any exception during
    record/stop is treated as a failure.

    Caveat (best-effort): if macOS feeds *silence* to a denied app instead of
    erroring, the file is still full-size, so this cannot distinguish "granted"
    from "denied-but-silent" — which is why we always print the Settings path.
    """
    try:
        recorder = recorder_factory()
        recorder.start()
        time.sleep(_PROBE_SECONDS)
        wav_path = recorder.stop()
    except Exception:
        return False
    try:
        return Path(wav_path).stat().st_size >= MIN_WAV_BYTES
    except OSError:
        return False


def _probe_listener() -> bool:
    """Construct and start a pynput keyboard Listener, then stop it.

    On macOS, capturing global key events requires BOTH Input Monitoring and
    Accessibility for the host terminal app. pynput surfaces a missing grant by
    raising on construction/start (or failing to run). We treat one successful
    start as evidence that the global-capture gate is open. This is monkeypatched
    in unit tests so we never touch the real OS there.
    """
    try:
        from pynput import keyboard

        listener = keyboard.Listener(on_press=lambda _k: None,
                                      on_release=lambda _k: None)
        listener.start()
        listener.stop()
        return True
    except Exception:
        return False


def check_permissions(*, recorder_factory: Callable[[], Recorder] = Recorder) -> PermissionStatus:
    microphone = _probe_microphone(recorder_factory)
    # Input Monitoring and Accessibility both gate the same global key-capture
    # behavior, so a single listener probe stands in for both.
    listener_ok = _probe_listener()
    return PermissionStatus(
        microphone=microphone,
        input_monitoring=listener_ok,
        accessibility=listener_ok,
    )


def hints(status: PermissionStatus) -> list[str]:
    """Human-readable fix steps for every permission that is False."""
    out: list[str] = []
    if not status.microphone:
        out.append(
            "Microphone: grant your terminal app under "
            "System Settings > Privacy & Security > Microphone"
        )
    if not status.input_monitoring:
        out.append(
            "Input Monitoring: grant your terminal app under "
            "System Settings > Privacy & Security > Input Monitoring"
        )
    if not status.accessibility:
        out.append(
            "Accessibility: grant your terminal app under "
            "System Settings > Privacy & Security > Accessibility"
        )
    return out
