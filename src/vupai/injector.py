"""Inject text into a tmux pane and confirm it landed before submitting.

INJECTION RULE: never sleep-then-Enter. Paste, poll capture-pane until the
pasted text appears, then send exactly one Enter. Retry the paste+poll once
on timeout; never send Enter blindly.

OPTIONAL REVIEW DELAY: `submit_delay` > 0 pauses AFTER the paste is confirmed and
BEFORE the Enter, giving the user a window to read the text and cancel a
mishearing by clearing the input (the confirmation needle disappears). If the
needle is gone after the pause, no Enter is sent and `inject` returns None
(cancelled - distinct from False, which means the paste was never confirmed).
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


def _present(pane_id: str, needle_n: str, io) -> bool:
    """Whether the (normalized) needle currently shows in the pane capture."""
    return needle_n in _norm(io.capture_pane(pane_id))


def _poll(
    pane_id: str,
    needle_n: str,
    *,
    confirm_timeout: float,
    poll_interval: float,
    io,
) -> bool:
    """Poll capture-pane until `needle_n` shows or `confirm_timeout` elapses.

    Does NOT paste - the caller owns pasting so a retry can avoid double-pasting.
    """
    deadline = time.monotonic() + confirm_timeout
    while True:
        if _present(pane_id, needle_n, io):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def _submit(pane_id: str, needle_n: str, submit_delay: float, io) -> bool | None:
    """Optionally pause for review, then send exactly one Enter.

    Returns True once Enter is sent. When `submit_delay` > 0, re-checks the
    needle after the pause: if the user cleared/changed the input during the
    window (needle gone), no Enter is sent and None is returned (cancelled).
    """
    if submit_delay > 0:
        time.sleep(submit_delay)
        if not _present(pane_id, needle_n, io):
            return None  # input cleared during the review window -> cancelled
    io.send_enter(pane_id)
    return True


def inject(
    pane_id: str,
    text: str,
    *,
    confirm_timeout: float = 2.0,
    poll_interval: float = 0.05,
    submit_delay: float = 0.0,
    io=tmuxio,
) -> bool | None:
    """Paste `text` into `pane_id`, confirm via capture-pane, then send Enter.

    Returns True on confirmed submit, False if the pasted text never appeared
    after one retry (Enter is NOT sent), or None if a `submit_delay` review
    window elapsed with the text cleared (cancelled, Enter NOT sent).
    """
    needle_n = _norm(_needle(text))
    if not needle_n:
        return False  # nothing to confirm -> never send a blind Enter
    for attempt in range(2):  # initial paste + exactly one retry
        if attempt > 0 and _present(pane_id, needle_n, io):
            # The first paste landed late (after the timeout). Re-pasting now
            # would duplicate the text and submit it twice; just confirm + submit.
            return _submit(pane_id, needle_n, submit_delay, io)
        io.load_buffer(text)
        io.paste_buffer(pane_id)
        if _poll(
            pane_id,
            needle_n,
            confirm_timeout=confirm_timeout,
            poll_interval=poll_interval,
            io=io,
        ):
            return _submit(pane_id, needle_n, submit_delay, io)
    return False
