"""Thin, exact wrappers over the tmux CLI.

Every helper builds the precise argv tmux expects and delegates execution to
``run``. ``run`` raises :class:`TmuxError` on a nonzero exit, surfacing stderr.
"""

from __future__ import annotations

import os
import subprocess

# Field 5 is the voice name, stored in the per-pane user option @voxpane_name
# (NOT pane_title): the target apps - Claude Code in particular - overwrite
# pane_title with their own string, but never touch @ user options. The option
# is empty when unset; registry.parse_panes falls back to the pane id there.
PANE_FORMAT = "\t".join(
    [
        "#{pane_id}",
        "#{window_id}",
        "#{window_name}",
        "#{pane_index}",
        "#{@voxpane_name}",
        "#{pane_current_command}",
        "#{pane_active}",
    ]
)


class TmuxError(RuntimeError):
    """Raised when a tmux command exits nonzero."""


def _base_argv() -> list[str]:
    # Tests may pin an isolated server via a private socket name.
    socket = os.environ.get("VTMUX_TMUX_SOCKET")
    if socket:
        return ["tmux", "-L", socket]
    return ["tmux"]


def run(args: list[str], *, stdin: str | None = None) -> str:
    """Run ``tmux <args>``; return stdout. Raise TmuxError on nonzero exit."""
    proc = subprocess.run(
        _base_argv() + args,
        input=stdin,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise TmuxError(proc.stderr.strip() or f"tmux {' '.join(args)} failed")
    return proc.stdout


def list_panes() -> list[str]:
    out = run(["list-panes", "-a", "-F", PANE_FORMAT])
    return [line for line in out.splitlines() if line.strip()]


def focused_pane_id() -> str | None:
    try:
        out = run(["display-message", "-p", "#{pane_id}"])
    except TmuxError:
        return None
    out = out.strip()
    return out or None


def load_buffer(text: str) -> None:
    run(["load-buffer", "-"], stdin=text)


def paste_buffer(pane_id: str) -> None:
    run(["paste-buffer", "-p", "-d", "-t", pane_id])


def capture_pane(pane_id: str) -> str:
    return run(["capture-pane", "-p", "-t", pane_id])


def send_enter(pane_id: str) -> None:
    run(["send-keys", "-t", pane_id, "Enter"])


def set_pane_name(pane_id: str, name: str) -> None:
    # Store the voice name in a per-pane user option the target app can't clobber
    # (unlike pane_title). Read back via @voxpane_name in PANE_FORMAT.
    run(["set", "-p", "-t", pane_id, "@voxpane_name", name])


def enable_pane_titles() -> None:
    run(["set", "-g", "pane-border-status", "top"])
    # Show the voice name when one is set, else fall back to the app's own title.
    run(["set", "-g", "pane-border-format",
         "#{?@voxpane_name,#{@voxpane_name},#{pane_title}}"])


def set_extended_keys_off() -> None:
    # Keep the CR from send-keys delivered as a plain Enter so Claude Code
    # submits on it. extended-keys (CSI-u) can re-encode Enter into an escape
    # the TUI does not treat as submit.
    run(["set", "-g", "extended-keys", "off"])


def display_message(pane_id: str, message: str) -> None:
    run(["display-message", "-t", pane_id, message])


def server_running() -> bool:
    try:
        run(["has-session"])
    except TmuxError:
        return False
    return True


def window_exists(name: str) -> bool:
    out = run(["list-windows", "-F", "#{window_name}"])
    return name in [line.strip() for line in out.splitlines()]


def new_window(name: str, command: str) -> None:
    run(["new-window", "-n", name, command])


def kill_window(name: str) -> None:
    run(["kill-window", "-t", name])


def attach() -> None:
    """Replace the current process with ``tmux attach``."""
    os.execvp("tmux", ["tmux", "attach"])
