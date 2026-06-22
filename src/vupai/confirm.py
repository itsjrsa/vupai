"""Confirm a destructive voice command with a tmux pop-up (display-popup).

Replaces the old "say the command, then say 'confirm'" flow. A single centered
popup appears over the panes; the user presses y (confirm) or n / Esc / nothing
(cancel). It is portable (any tmux >= 3.2) and stays in the terminal - no
macOS-specific dialog. The prompt also tells the user how to turn confirmations
off in the config.

`run` is injected so the daemon's confirm logic is unit-tested with a fake; the
default runner shells out to `tmux display-popup`. Fail-safe: any failure (no
client, old tmux, the popup closing without an answer) returns False (cancel) -
a destructive action never proceeds on a broken/timed-out prompt.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from vupai import tmuxio

logger = logging.getLogger(__name__)


def _build_script(summary: str, timeout: float, result_path: str) -> str:
    """The shell run inside the popup: show the prompt, read one key, write the
    choice ('y'/'n') to `result_path`. `read -t` self-dismisses on no input."""
    lines = [
        f"{summary}?",
        "",
        "  [y] confirm     [n] cancel",
        "",
        "  (disable: set confirm_destructive = false in",
        "   ~/.config/vupai/config.toml)",
    ]
    printf = "printf '%s\\n' " + " ".join(shlex.quote(ln) for ln in lines)
    return (
        f"{printf}; "
        f"read -rsn1 -t {int(timeout)} k; "
        f'case "$k" in y|Y) printf y;; *) printf n;; esac > {shlex.quote(result_path)}'
    )


def _build_argv(summary: str, timeout: float, result_path: str) -> list[str]:
    # bash -c so the single-key `read -rsn1` works regardless of tmux's wrapping
    # shell (POSIX sh lacks read -n/-s/-t). macOS/Linux ship bash; if it is
    # missing the popup fails and we fail-safe to cancel.
    inner = f"bash -c {shlex.quote(_build_script(summary, timeout, result_path))}"
    return tmuxio._base_argv() + [
        "display-popup", "-E", "-w", "64", "-h", "13",
        "-T", " vupai - confirm ", inner,
    ]


def _default_run(argv: list[str], *, result_path: str, timeout: float) -> None:
    # display-popup -E blocks until the popup closes; the inner read self-dismisses
    # after `timeout`, so cap the subprocess a little beyond that.
    subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 5)


def popup_confirm(summary: str, *, timeout: float = 8.0,
                  run=_default_run, tmpdir: Path | None = None) -> bool:
    """Show the confirmation popup for `summary`; True iff the user confirmed."""
    base = tmpdir if tmpdir is not None else Path(tempfile.gettempdir())
    fd, name = tempfile.mkstemp(prefix="vupai-confirm-", dir=base)
    result_path = Path(name)
    os.close(fd)
    result_path.unlink(missing_ok=True)  # the popup (re)creates it on answer
    try:
        argv = _build_argv(summary, timeout, str(result_path))
        try:
            run(argv, result_path=str(result_path), timeout=timeout)
        except Exception:
            logger.warning("confirm popup failed to run; cancelling", exc_info=True)
            return False
        try:
            return result_path.read_text().strip() == "y"
        except OSError:
            return False  # no answer written (dismissed/crashed) -> cancel
    finally:
        result_path.unlink(missing_ok=True)
