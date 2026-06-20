"""Best-effort macOS permission probing.

NOTE: macOS TCC (the privacy database) is NOT readable from Python. We cannot
ask the OS "do we hold the Microphone permission?". So every check here is an
*indirect behavioral probe*: we try to do the thing and infer the permission
from success/failure. Results can have false negatives (e.g. a genuinely silent
room) — we always print the exact System Settings panes so the user can verify
and grant manually.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

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


# macOS cannot grant TCC permissions programmatically, but it *can* deep-link
# straight to the relevant pane. Each spec maps a PermissionStatus field to its
# human label, the System Settings anchor (used both in the x-apple URL and the
# `tccutil reset <service>` command), where the anchor doubles as the tccutil
# service name. Order is the order we surface fixes in.
_URL_BASE = "x-apple.systempreferences:com.apple.preference.security?"
_PERM_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("microphone", "Microphone", "Privacy_Microphone", "Microphone"),
    ("input_monitoring", "Input Monitoring", "Privacy_ListenEvent", "ListenEvent"),
    ("accessibility", "Accessibility", "Privacy_Accessibility", "Accessibility"),
)


@dataclass(frozen=True)
class PermissionFix:
    field: str           # PermissionStatus attribute, e.g. "microphone"
    label: str           # "Microphone"
    url: str             # x-apple.systempreferences: deep link to the pane
    reset_service: str   # `tccutil reset <service> <bundle>` service name


def fixes(status: PermissionStatus) -> list[PermissionFix]:
    """A PermissionFix for every permission that is currently False."""
    return [
        PermissionFix(field, label, _URL_BASE + anchor, service)
        for field, label, anchor, service in _PERM_SPECS
        if not getattr(status, field)
    ]


@dataclass(frozen=True)
class TerminalApp:
    name: str
    bundle_id: str | None


# TERM_PROGRAM value -> (display name, bundle id). macOS attaches TCC grants to
# the *terminal app* (the daemon's "responsible process"), so naming it exactly
# beats the generic "your terminal app" and lets us emit a precise tccutil reset.
_TERMINAL_APPS: dict[str, tuple[str, str]] = {
    "Apple_Terminal": ("Terminal", "com.apple.Terminal"),
    "iTerm.app": ("iTerm", "com.googlecode.iterm2"),
    "ghostty": ("Ghostty", "com.mitchellh.ghostty"),
    "WezTerm": ("WezTerm", "com.github.wez.wezterm"),
    "vscode": ("Visual Studio Code", "com.microsoft.VSCode"),
    "Hyper": ("Hyper", "co.zeit.hyper"),
    "kitty": ("kitty", "net.kovidgoyal.kitty"),
    "Alacritty": ("Alacritty", "org.alacritty"),
    "Tabby": ("Tabby", "org.tabby"),
    "rio": ("Rio", "com.raphaelamorim.rio"),
}


def terminal_app(env: Mapping[str, str] | None = None) -> TerminalApp:
    """Identify the host terminal app from the environment (best-effort).

    Falls back to ``__CFBundleIdentifier`` (set by macOS LaunchServices) for an
    unrecognized TERM_PROGRAM, and finally to a generic placeholder so callers
    always get a usable display name.
    """
    env = os.environ if env is None else env
    term = env.get("TERM_PROGRAM", "") or ""
    if term in _TERMINAL_APPS:
        name, bundle = _TERMINAL_APPS[term]
        return TerminalApp(name, bundle)
    bundle = env.get("__CFBundleIdentifier") or None
    return TerminalApp(term or bundle or "your terminal app", bundle)


def open_settings_pane(url: str, *, runner: Callable[..., object] = subprocess.run) -> bool:
    """Open a System Settings deep link via `open <url>`. Best-effort."""
    try:
        runner(["open", url], check=False)
        return True
    except Exception:
        return False


def hints(status: PermissionStatus, *, app: TerminalApp | None = None) -> list[str]:
    """Human-readable fix steps for every permission that is False.

    One line per failing permission. When ``app`` is given, the line names the
    actual terminal app and includes the deep-link URL to its pane.
    """
    who = app.name if app else "your terminal app"
    return [
        f"{fix.label}: grant {who} under "
        f"System Settings > Privacy & Security > {fix.label}  "
        f"(open: {fix.url})"
        for fix in fixes(status)
    ]
