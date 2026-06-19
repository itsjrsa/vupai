"""Inject text into a tmux pane and confirm it landed before submitting.

INJECTION RULE: never sleep-then-Enter. Paste, poll capture-pane until the
pasted text appears, then send exactly one Enter. Retry the paste+poll once
on timeout; never send Enter blindly.
"""

from __future__ import annotations

import time

from vtmux import tmuxio

_NEEDLE_MAX = 40  # use the trailing <=40 chars of the last line as the confirmation needle


def _needle(text: str) -> str:
    """Trailing <=40 chars of the last non-empty-stripped line of `text`."""
    lines = text.splitlines() or [text]
    last = lines[-1].strip()
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
    deadline = time.monotonic() + confirm_timeout
    while True:
        if needle in io.capture_pane(pane_id):
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
