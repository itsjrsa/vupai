"""Rotating status-bar tips: ambient discoverability for the voice grammar.

vupai has no on-screen menu (it is voice-driven), so users forget the available
commands - especially their own macros and slash verbs. `build_tips` renders a
config-generated pool of short example sentences; `TipRotator` cycles them into
the tmux status-left segment (see tmuxio.install_tip_segment).

MAINTENANCE: when a new spoken command, slash verb, macro, or program is added,
consider adding a matching example here and confirm with the user whether it
belongs in the rotation (see AGENTS.md). The verb examples reuse the parser's
verb constants so they cannot drift from commands.py.
"""
from __future__ import annotations

import logging
import threading

from vupai import tmuxio
from vupai.commands import _ACTIVITY_VERBS, _CLOSE_VERBS, _CREATE_VERBS

logger = logging.getLogger(__name__)

_TIP_PREFIX = "tip: "
_TIP_MAX = 48  # truncate so the status-left segment stays compact
_ELLIPSIS = "…"


def _render(text: str) -> str:
    """Prefix and length-cap a tip. When it overflows _TIP_MAX, truncate on a
    word boundary and append an ellipsis so the tip never ends mid-word (e.g.
    "...to hide" must not render as "...to hid")."""
    full = _TIP_PREFIX + text
    if len(full) <= _TIP_MAX:
        return full
    clipped = full[: _TIP_MAX - 1].rstrip()
    # Back off to a word boundary, but only within the body: a single
    # space-less word (e.g. a long macro name) is hard-cut rather than erased.
    cut = clipped.rfind(" ")
    if cut > len(_TIP_PREFIX):
        clipped = clipped[:cut]
    return clipped + _ELLIPSIS


def _interleave(a: list[str], b: list[str]) -> list[str]:
    """Weave two lists so consecutive items differ in kind; trailing remainder
    of the longer list is appended in order. Deterministic (no RNG)."""
    out: list[str] = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
    return out


def build_tips(cfg) -> list[str]:
    """Build the rotating tip pool for `cfg`. Deterministic order, never empty.

    Shows command examples (static verbs + the user's slash verbs, macros, and
    programs) alongside the broadcast and usage hints.
    """
    keys = cfg.command_hotkey
    talk_key = keys[0] if keys else "?"   # show the primary key in tips
    hints = [
        f"hold {talk_key} to talk",
        "set status_tips=false to hide these",
    ]
    commands: list[str] = [
        f"{_CREATE_VERBS[0]} two panes",
        "focus nova",
        "zoom nova",
        f"{_CLOSE_VERBS[0]} nova",
        f"{_CLOSE_VERBS[0]} nova and atlas",  # and-joined multi-target list
        "swap nova and atlas",
        "open board",
        _ACTIVITY_VERBS[0],  # "activity": who is editing what across panes
        "ssh vm1",  # ssh to a configured host (hosts.toml)
    ]
    commands += [f"{verb} all" for verb in sorted(cfg.slash_commands)]
    commands += list(cfg.macros)
    if cfg.programs:
        commands.append(f"create one {sorted(cfg.programs)[0]} pane")
    # Subset broadcast (a leading "and"-joined name run + a message) is a
    # routing extension of broadcast, so include it too.
    commands.append(f"{cfg.broadcast_word} ship it")
    commands.append("nova and atlas, run tests")
    return [_render(t) for t in _interleave(commands, hints)]


class TipRotator:
    """Cycle a tip pool into tmux status-left on a fixed interval.

    Mirrors watcher.PaneWatcher: a daemon background thread with an interruptible
    wait. ISOLATION: touches ONLY tmuxio.set_tip - never the recorder, ASR, the
    injector, or the daemon jobs queue. Best-effort: a tmux failure on a tick is
    swallowed so the rotator can never break the voice pipeline.
    """

    def __init__(self, tips: list[str], *, interval: float = 15.0, io=tmuxio) -> None:
        self._tips = list(tips)
        self._interval = interval
        self._io = io
        self._i = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        if not self._tips:
            return
        tip = self._tips[self._i % len(self._tips)]
        self._i += 1
        try:
            self._io.set_tip(tip)
        except Exception:
            logger.debug("tip rotator set_tip failed", exc_info=True)

    def start(self) -> None:
        if self._thread is not None or not self._tips:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            # Interruptible sleep: stop() wakes it at once, so teardown is prompt.
            if self._stop.wait(self._interval):
                break

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            # Blank the tip so a stopped daemon doesn't leave a stale suggestion
            # pinned in status-left; the #{@vupai_tip} format then collapses to
            # the session/window list. Best-effort, like tick().
            try:
                self._io.set_tip("")
            except Exception:
                logger.debug("tip rotator clear-on-stop failed", exc_info=True)
