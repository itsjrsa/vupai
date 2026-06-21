"""Microphone enumeration for macOS.

sox's `rec` records from the system default input or, when AUDIODEV is set in
its environment, from the named CoreAudio device (see recorder.py). sox has no
portable "list devices" call, so we enumerate input devices from
`system_profiler -json SPAudioDataType` and match the configured name against
that list. CoreAudio device names line up with sox's AUDIODEV value.

Enumeration shells out to `system_profiler`, which takes ~1s; callers resolve
the device ONCE (CLI listing, daemon startup) - never per key-press.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class InputDevice:
    name: str
    is_default: bool


def _default_runner() -> str:
    return subprocess.run(
        ["system_profiler", "-json", "SPAudioDataType"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    ).stdout


def list_input_devices(*, runner=None) -> list[InputDevice]:
    """Return the present CoreAudio input devices, system default flagged.

    Best-effort: any failure (system_profiler missing, non-JSON output) yields
    an empty list rather than raising, so callers degrade to "system default".
    """
    runner = runner if runner is not None else _default_runner
    try:
        raw = runner()
    except Exception:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []

    devices: list[InputDevice] = []
    for group in data.get("SPAudioDataType", []):
        for item in group.get("_items", []):
            # Only items exposing an input channel count are microphones.
            if "coreaudio_device_input" not in item:
                continue
            name = item.get("_name")
            if not name:
                continue
            is_default = (
                item.get("coreaudio_default_audio_input_device") == "spaudio_yes"
            )
            devices.append(InputDevice(name=name, is_default=is_default))
    return devices


def resolve_device(configured: str, *, runner=None) -> tuple[str, str | None]:
    """Resolve a configured mic name against the devices present right now.

    Returns ``(device_to_use, warning)``. An empty `configured` means "system
    default" -> ``("", None)`` with no enumeration. A configured name that is
    present is returned verbatim. A configured name that is absent falls back to
    the system default (``""``) with a human-readable warning string.
    """
    if not configured:
        return "", None
    names = [d.name for d in list_input_devices(runner=runner)]
    if configured in names:
        return configured, None
    return "", f"configured mic {configured!r} not found; using system default"
