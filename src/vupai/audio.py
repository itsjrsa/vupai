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
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


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


def _default_capture_probe(device: str) -> tuple[int, str, int]:
    """Record a brief clip with `rec` and report (returncode, stderr, wav bytes).

    Mirrors recorder.py's argv so the probe exercises the same path the daemon
    will. An empty `device` records from the system default; otherwise AUDIODEV
    pins the named CoreAudio device. The clip is short (the probe only needs to
    confirm the stream opens and produces samples) and is always cleaned up.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    path = Path(tmp.name)
    env = None
    if device:
        env = {**os.environ, "AUDIODEV": device}
    try:
        proc = subprocess.run(
            ["rec", "-q", "-c", "1", "-r", "16000", "-b", "16",
             str(path), "trim", "0", "0.4"],
            capture_output=True, text=True, env=env, timeout=15,
        )
        size = path.stat().st_size if path.exists() else 0
        return proc.returncode, proc.stderr, size
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def probe_capture(device: str, *, runner=None) -> str | None:
    """Verify sox can actually capture audio from `device`.

    Returns None when a brief recording succeeds, else a human-readable reason
    the device is unusable. This catches failures that mere enumeration cannot:
    a name that collides with an output-only device (some USB mics expose a
    speaker and a mic under the *same* name, and sox's AUDIODEV name-match grabs
    the output), a disconnected device, a muted input, or a missing Microphone
    permission. `runner(device) -> (returncode, stderr, wav_bytes)` is injectable
    for tests. An empty `device` probes the system default.
    """
    from .recorder import MIN_WAV_BYTES

    runner = runner if runner is not None else _default_capture_probe
    label = device or "system default"
    try:
        returncode, stderr, size = runner(device)
    except Exception as exc:  # probe must never crash the caller
        return f"could not probe {label!r}: {exc}"
    if returncode != 0:
        lines = [ln for ln in (stderr or "").splitlines() if ln.strip()]
        detail = lines[-1].strip() if lines else f"rec exited {returncode}"
        return f"cannot record from {label!r}: {detail}"
    if size < MIN_WAV_BYTES:
        return (
            f"recording from {label!r} produced no audio ({size} bytes); "
            "the device may share its name with an output, be muted, or lack "
            "Microphone permission"
        )
    return None
