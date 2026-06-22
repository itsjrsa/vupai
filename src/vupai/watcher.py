"""Agent-state poller: notify when a watched agent pane goes busy -> idle.

vupai is otherwise input-only; nothing pings back when an agent finishes. This
runs a background thread that, on each tick, captures the tail of every NAMED
pane, classifies it (working / idle / unknown), and fires a macOS notification
on the busy -> idle edge (the "agent is done" signal).

ISOLATION: this thread touches ONLY tmux capture-pane (via its OWN PaneRegistry,
never the daemon's) plus osascript. It must NEVER touch the recorder, ASR/MLX,
the injector, or the daemon's jobs queue - sharing the daemon's registry would
race its refresh, and any MLX call off the main thread breaks the GPU stream.

v1 scope: WORKING/IDLE detection and notification only. The y/n permission-prompt
("awaiting input") classification and the audio chime are deferred until the
busy/idle heuristic is validated against a live Claude TUI; the heuristic is the
one version-fragile surface and is isolated in `classify_state` for that reason.
"""
from __future__ import annotations

import enum
import logging
import subprocess
import threading
import time

from vupai import tmuxio

logger = logging.getLogger(__name__)


class PaneState(enum.Enum):
    WORKING = "working"   # the agent is generating (busy)
    IDLE = "idle"         # back at the prompt, ready for input
    UNKNOWN = "unknown"   # can't tell from the captured tail -> never notify


# Heuristic anchors in Claude Code's TUI. FRAGILE across versions - the entire
# version risk is concentrated here so a UI change is a one-edit fix. WORKING
# wins over IDLE (both can momentarily appear during a redraw).
_WORKING_MARKERS = ("esc to interrupt",)
_IDLE_MARKERS = ("? for shortcuts",)


def classify_state(tail_text: str) -> PaneState:
    """Classify a pane's captured tail. Pure (no IO) and fixture-tested."""
    low = tail_text.lower()
    if any(m in low for m in _WORKING_MARKERS):
        return PaneState.WORKING
    if any(m in low for m in _IDLE_MARKERS):
        return PaneState.IDLE
    return PaneState.UNKNOWN


def _osascript_quote(text: str) -> str:
    """Quote a string for an AppleScript literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript_notify(title: str, message: str, *, runner=subprocess.run) -> None:
    """Fire a macOS notification. Best-effort: every failure is swallowed."""
    script = (f"display notification {_osascript_quote(message)} "
              f"with title {_osascript_quote(title)}")
    try:
        runner(["osascript", "-e", script],
               capture_output=True, text=True, timeout=5)
    except Exception:
        logger.debug("osascript notification failed", exc_info=True)


class PaneWatcher:
    """Polls named panes and notifies on the busy -> idle transition.

    All collaborators are injected so the unit suite drives ticks deterministically
    with fakes (no thread, no tmux, no osascript). The poll loop uses an Event so
    `stop()` interrupts a pending wait immediately (never hangs `vupai down`).
    """

    def __init__(self, registry, *, capture_fn=tmuxio.capture_pane,
                 notifier=_osascript_notify, chimer=None,
                 clock=time.monotonic, poll_interval: float = 2.0,
                 capture_lines: int = 12, debounce: float = 0.0,
                 classify_fn=classify_state) -> None:
        self._registry = registry
        self._capture_fn = capture_fn
        self._notifier = notifier
        self._chimer = chimer            # zero-arg callable, or None (no chime)
        self._clock = clock
        self._poll_interval = poll_interval
        self._capture_lines = capture_lines
        self._debounce = debounce
        self._classify = classify_fn
        self._prev: dict[str, PaneState] = {}     # pane id -> last seen state
        self._last_notify: dict[str, float] = {}  # pane id -> last notify clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        """One synchronous poll cycle. Safe to call directly in tests."""
        try:
            self._registry.refresh()
        except Exception:
            logger.debug("watcher registry refresh failed", exc_info=True)
            return
        # Watch named panes only; an unnamed pane (name == id) is a plain shell.
        panes = [p for p in self._registry.panes if p.name != p.id]
        seen: set[str] = set()
        for pane in panes:
            seen.add(pane.id)
            try:
                text = self._capture_fn(pane.id)
            except Exception:
                # Pane vanished mid-tick (closed/swapped); skip it, keep going.
                continue
            if self._capture_lines:
                text = "\n".join(text.splitlines()[-self._capture_lines:])
            state = self._classify(text)
            prev = self._prev.get(pane.id)
            self._prev[pane.id] = state
            if prev is None or state == PaneState.UNKNOWN:
                # First observation establishes a baseline; UNKNOWN is never a
                # notify edge (don't fire on a cleared screen / noise).
                continue
            if prev == PaneState.WORKING and state == PaneState.IDLE:
                self._maybe_notify(pane)
        # Drop panes that disappeared so a recreated id starts fresh.
        for pid in list(self._prev):
            if pid not in seen:
                self._prev.pop(pid, None)
                self._last_notify.pop(pid, None)

    def _maybe_notify(self, pane) -> None:
        now = self._clock()
        last = self._last_notify.get(pane.id)
        if last is not None and (now - last) < self._debounce:
            return
        self._last_notify[pane.id] = now
        try:
            self._notifier("vupai", f"{pane.name} is ready for input")
        except Exception:
            logger.debug("watcher notifier failed", exc_info=True)
        if self._chimer is not None:
            try:
                self._chimer()
            except Exception:
                logger.debug("watcher chimer failed", exc_info=True)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            # Interruptible sleep: stop() wakes it at once, so teardown is prompt.
            if self._stop.wait(self._poll_interval):
                break

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None
