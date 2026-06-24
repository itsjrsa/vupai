"""Swappable, best-effort text-to-speech sink: the "talk back" half of vupai.

`speak` shells out to `tts_cmd` (default macOS `say`), passing the text as ONE
argv argument (never stdin, never a shell) - the same agnostic lowest-common-
denominator contract as summarize.summarize. The same rule lets `say`, an
external neural-TTS CLI, or `espeak` all satisfy it unmodified.

Crucially the process is fired and NOT awaited: `say` blocks until the phrase
finishes (seconds), and the daemon's command path must never block that long, so
`speak` returns the Popen handle immediately (or None on failure). Holding the
handle also leaves room for a future barge-in (terminate on the next push-to-talk).

Every failure - command not found, a spawn error, empty text - is swallowed
(best-effort, mirrors watcher._osascript_notify): speech must never break the
voice pipeline.
"""
from __future__ import annotations

import logging
import shlex
import subprocess

logger = logging.getLogger(__name__)

__all__ = ["speak"]


def speak(text: str, *, cmd: str = "say", spawn=subprocess.Popen):
    """Speak `text` via `cmd`, non-blocking. Returns the process handle or None.

    `spawn` is injected so the unit suite asserts the argv without spawning a real
    process. A blank `text` or empty `cmd` is a no-op (returns None); any spawn
    failure is logged at debug and swallowed.
    """
    text = (text or "").strip()
    if not text:
        return None
    argv = shlex.split(cmd)
    if not argv:
        return None
    argv.append(text)
    try:
        return spawn(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        logger.debug("tts command not found: %s", argv[0])
    except Exception:
        logger.debug("tts failed to spawn", exc_info=True)
    return None
