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
import queue
import re
import shlex
import subprocess
import threading

logger = logging.getLogger(__name__)

__all__ = ["speak", "SentenceSpeaker", "split_sentences"]


# A sentence ends at one-or-more . ! ? FOLLOWED BY whitespace (so a terminator at
# the very end of the buffer-so-far waits for the next chunk instead of splitting
# prematurely mid-stream), or at a newline. "2.0" / "session.py" don't split
# because the dot is followed by a non-space.
_BOUNDARY = re.compile(r"[.!?]+(?=\s)|\n")


def split_sentences(buf: str) -> tuple[list[str], str]:
    """Split `buf` into (complete_sentences, remainder).

    Only boundaries with trailing whitespace already present count, so a buffer
    ending in "..." or "bug." holds that fragment back until the next chunk
    confirms the break (or `SentenceSpeaker.close` flushes it). Mirrors the
    spoken-length cap in summarize._spoken, but incrementally.
    """
    out: list[str] = []
    pos = 0
    for m in _BOUNDARY.finditer(buf):
        seg = buf[pos:m.end()].strip()
        if seg:
            out.append(seg)
        pos = m.end()
    return out, buf[pos:]


class SentenceSpeaker:
    """Speak streamed text sentence-by-sentence, in order, without overlap.

    Streaming summarizers emit text as the model generates it; this buffers those
    chunks, flushes each complete sentence to `speak_one`, and a single worker
    thread plays them one at a time - waiting for each utterance's process to
    finish before starting the next, so they never talk over each other. The
    point is latency: the first sentence is spoken ~1-2s in, while the rest is
    still being generated, instead of waiting for the whole reply.

    `speak_one(text)` plays one phrase and returns a process handle with `.wait()`
    (or None when muted/failed). Routing through it (rather than calling `say`
    directly) keeps the daemon's runtime mute switch in charge of streamed speech
    too. Best-effort throughout: a failed utterance is skipped, never raised.
    """

    def __init__(self, speak_one, *, join_on_close: bool = True,
                 cancel: "threading.Event | None" = None,
                 max_sentences: int | None = None):
        self._speak_one = speak_one
        self._join = join_on_close
        # A shared cancel signal (the daemon sets it on barge-in). Default to a
        # private, never-set Event so the un-cancelled path behaves as before.
        self._cancel = cancel if cancel is not None else threading.Event()
        # Length cap: enqueue at most this many sentences, drop the rest. None =
        # uncapped (the board digest, whose length tracks the agent count).
        self._max = max_sentences
        self._count = 0
        self._buf = ""
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False

    def _ensure_started(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:  # sentinel: drain done
                return
            if self._cancel.is_set():
                continue  # interrupted: drain the queue without speaking
            try:
                proc = self._speak_one(item)
            except Exception:
                logger.debug("streamed speak failed", exc_info=True)
                proc = None
            if proc is not None:
                try:
                    proc.wait()  # serialize: no overlapping audio. A barge-in
                    # terminates this proc (daemon._silence), so wait() returns.
                except Exception:
                    pass

    def feed(self, text: str) -> None:
        """Append streamed text; enqueue any complete sentences it completes.

        No-op once cancelled. Stops enqueuing once `max_sentences` is reached and
        drops the buffered remainder so a chatty model can't run past the cap.
        """
        if self._cancel.is_set() or not text:
            return
        if self._max is not None and self._count >= self._max:
            self._buf = ""
            return
        self._buf += text
        sentences, self._buf = split_sentences(self._buf)
        for s in sentences:
            if self._max is not None and self._count >= self._max:
                self._buf = ""  # cap hit mid-flush: drop the tail
                return
            self._ensure_started()
            self._q.put(s)
            self._count += 1

    def close(self) -> None:
        """Flush the trailing fragment, then wait for playback to finish.

        A cancelled or capped-out speaker flushes nothing (the remainder is past
        the budget or explicitly interrupted), but still drains the worker.
        """
        remainder = self._buf.strip()
        self._buf = ""
        capped = self._max is not None and self._count >= self._max
        if remainder and not self._cancel.is_set() and not capped:
            self._ensure_started()
            self._q.put(remainder)
            self._count += 1
        if self._started:
            self._q.put(None)
            if self._join:
                self._thread.join()


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
