"""Inject text into a tmux pane and confirm it landed before submitting.

INJECTION RULE: never sleep-then-Enter. Paste, poll capture-pane until the
pasted text appears, then send exactly one Enter. Retry the paste+poll once
on timeout; never send Enter blindly.
"""

from __future__ import annotations

import re
import time

from vupai import tmuxio

_NEEDLE_MAX = 40  # use the trailing <=40 chars of the last line as the confirmation needle

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Collapse every run of whitespace (incl. newlines) to a single space.

    The target app word-wraps a long pasted line across several drawn rows with
    its own indentation; tmux `capture-pane -J` only joins tmux's *own* wrap, not
    the app's. Normalising both needle and capture this way lets the needle match
    even when it straddles such an app-drawn wrap (the inter-word break becomes a
    single space either way)."""
    return _WS.sub(" ", text).strip()


def _needle(text: str) -> str:
    """Trailing <=40 chars of the last non-empty (after strip) line of `text`."""
    lines = [ln for ln in text.splitlines() if ln.strip()] or [""]
    last = lines[-1]
    return last[-_NEEDLE_MAX:]


def _paste_and_poll(
    pane_id: str,
    text: str,
    needle: str,
    *,
    confirm_timeout: float,
    poll_interval: float,
    io,
) -> bool:
    """Load+paste once, then poll capture-pane until `needle` shows or timeout."""
    io.load_buffer(text)
    io.paste_buffer(pane_id)
    needle_n = _norm(needle)
    deadline = time.monotonic() + confirm_timeout
    while True:
        if needle_n in _norm(io.capture_pane(pane_id)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def inject(
    pane_id: str,
    text: str,
    *,
    confirm_timeout: float = 2.0,
    poll_interval: float = 0.05,
    io=tmuxio,
) -> bool:
    """Paste `text` into `pane_id`, confirm via capture-pane, then send Enter.

    Returns True on confirmed submit, False if the pasted text never appeared
    after one retry (Enter is NOT sent in that case).
    """
    needle = _needle(text)
    for _attempt in range(2):  # initial try + exactly one retry
        if _paste_and_poll(
            pane_id,
            text,
            needle,
            confirm_timeout=confirm_timeout,
            poll_interval=poll_interval,
            io=io,
        ):
            io.send_enter(pane_id)
            return True
    return False
