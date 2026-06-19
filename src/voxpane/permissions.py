"""Best-effort macOS permission probing.

NOTE: macOS TCC (the privacy database) is NOT readable from Python. We cannot
ask the OS "do we hold the Microphone permission?". So every check here is an
*indirect behavioral probe*: we try to do the thing and infer the permission
from success/failure. Results can have false negatives (e.g. a genuinely silent
room) — we always print the exact System Settings panes so the user can verify
and grant manually.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .recorder import MIN_WAV_BYTES, Recorder


@dataclass(frozen=True)
class PermissionStatus:
    microphone: bool
    input_monitoring: bool
    accessibility: bool


def _probe_microphone(recorder_factory: Callable[[], Recorder]) -> bool:
    """Record a very short clip and assert the wav is non-trivial in size.

    If sox cannot access the mic (permission denied) the resulting file is empty
    or header-only. Any exception during record/stop is treated as a failure.
    """
    try:
        recorder = recorder_factory()
        recorder.start()
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
