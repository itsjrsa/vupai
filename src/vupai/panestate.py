"""Pane state classification shared by the watcher and the supervision board.

Two classifiers live here:

- `classify_state` is the watcher's original marker test (Claude Code TUI
  anchors). Pure, 3-state (WORKING / IDLE / UNKNOWN). Kept here so `watcher.py`
  re-exports it unchanged.

- `ChurnClassifier` is the board's TOOL-AGNOSTIC baseline: it diffs each pane's
  captured tail over time and infers WORKING vs IDLE from how much changed, with
  a dead-band (hysteresis) so redraw flicker does not flap the state and a settle
  streak so a brief pause is not mistaken for "done". Per-tool MARKERS are an
  OPTIONAL refinement layered on top (they override an ambiguous frame and let a
  pane settle in one tick); a tool with no markers still classifies purely from
  churn. That is the agnosticism guarantee: nothing here is Claude-only.

`detect_needs_input` is a generic, high-precision/low-recall heuristic for the
cosmetic NEEDS_INPUT annotation; it never invents a state from a working frame.
"""
from __future__ import annotations

import difflib
import enum
import hashlib
import re
from dataclasses import dataclass


class PaneState(enum.Enum):
    WORKING = "working"        # the agent is generating (busy)
    IDLE = "idle"              # back at the prompt, settled
    NEEDS_INPUT = "needs_input"  # settled, and apparently awaiting an answer
    UNKNOWN = "unknown"        # can't tell yet (baseline / no signal)


# Heuristic anchors in Claude Code's TUI. FRAGILE across versions - the entire
# version risk is concentrated here so a UI change is a one-edit fix. WORKING
# wins over IDLE (both can momentarily appear during a redraw).
_WORKING_MARKERS = ("esc to interrupt",)
_IDLE_MARKERS = ("? for shortcuts",)


def classify_state(tail_text: str) -> PaneState:
    """Classify a pane's captured tail by marker. Pure (no IO), fixture-tested.

    The watcher's original 3-state classifier; returns UNKNOWN for any TUI whose
    markers are absent (which is why the board uses churn as its baseline).
    """
    low = tail_text.lower()
    if any(m in low for m in _WORKING_MARKERS):
        return PaneState.WORKING
    if any(m in low for m in _IDLE_MARKERS):
        return PaneState.IDLE
    return PaneState.UNKNOWN


# --- tool-agnostic churn classifier ----------------------------------------

# Tuning constants. Exposed as ChurnClassifier defaults (overridable per
# instance for tests) rather than config keys, to keep the TOML surface small.
CHURN_ACTIVE = 0.10   # tail changed >= this fraction since last tick -> WORKING
CHURN_IDLE = 0.01     # tail changed <= this fraction -> IDLE; between = hold
SETTLE_TICKS = 2      # consecutive IDLE ticks before a WORKING->IDLE edge fires


@dataclass(frozen=True)
class Markers:
    """Optional per-tool refinement strings (all lowercased substrings)."""
    working: tuple[str, ...] = ()
    idle: tuple[str, ...] = ()
    needs_input: tuple[str, ...] = ()


_EMPTY_MARKERS = Markers()

# Keyed by the program label vupai stores in @vupai_program (e.g. "claude").
# Only Claude ships markers in v1; every other tool falls back to pure churn,
# which is the whole point. Add entries as other TUIs are observed.
MARKERS: dict[str, Markers] = {
    "claude": Markers(
        working=_WORKING_MARKERS,
        idle=_IDLE_MARKERS,
        needs_input=("do you want",),
    ),
}


def markers_for(program: str | None) -> Markers:
    """Marker set for a program label, or an empty set (pure-churn) if unknown."""
    return MARKERS.get((program or "").strip().lower(), _EMPTY_MARKERS)


def churn(prev_tail: str | None, tail: str) -> float:
    """Fraction of the tail that changed since the previous capture, in [0, 1].

    First observation (prev is None) reports 0.0: with no diff to go on we treat
    the pane as quiet and let the next tick (or a marker) reveal real activity,
    rather than guessing WORKING and risking a spurious settle edge.
    """
    if prev_tail is None:
        return 0.0
    if prev_tail == tail:
        return 0.0
    return 1.0 - difflib.SequenceMatcher(None, prev_tail, tail).ratio()


@dataclass
class Observation:
    """Result of one ChurnClassifier.observe() call."""
    state: PaneState        # settled, externally-visible state (held until proven)
    raw: PaneState          # this tick's raw (pre-settle) classification
    churn: float            # fraction changed since last tick
    settled_edge: bool      # True exactly on a WORKING -> IDLE settle transition
    content_hash: bytes     # hash of the tail; the summary cache/skip key


class ChurnClassifier:
    """Stateful per-pane classifier: churn baseline + optional marker refinement.

    Feed it the captured tail each tick via `observe`. It tracks the previous
    tail, the raw state (with hysteresis), and an IDLE streak so a WORKING->IDLE
    transition only fires after the pane has been quiet for `settle_ticks`
    consecutive ticks (or one tick when an IDLE marker confirms the tool is done).
    """

    def __init__(self, *, markers: Markers = _EMPTY_MARKERS,
                 active: float = CHURN_ACTIVE, idle: float = CHURN_IDLE,
                 settle_ticks: int = SETTLE_TICKS) -> None:
        self._markers = markers
        self._active = active
        self._idle = idle
        self._settle_ticks = max(1, settle_ticks)
        self._prev_tail: str | None = None
        self._raw = PaneState.UNKNOWN       # last raw classification
        self._settled = PaneState.UNKNOWN   # last settled state (visible)
        self._idle_streak = 0

    def _raw_state(self, tail: str, ratio: float) -> tuple[PaneState, bool]:
        """Raw state for this tick plus whether an IDLE marker confirmed it.

        Markers override the churn verdict: a WORKING marker forces WORKING even
        on a quiet frame (the tool is busy but hasn't redrawn), and an IDLE
        marker both forces IDLE and lets the pane settle in a single tick.
        """
        low = tail.lower()
        if any(m in low for m in self._markers.working):
            return PaneState.WORKING, False
        idle_marker = any(m in low for m in self._markers.idle)
        if idle_marker:
            return PaneState.IDLE, True
        if ratio >= self._active:
            return PaneState.WORKING, False
        if ratio <= self._idle:
            return PaneState.IDLE, False
        return self._raw, False  # dead-band: hold prior raw, no flap

    def observe(self, tail: str) -> Observation:
        ratio = churn(self._prev_tail, tail)
        self._prev_tail = tail
        raw, idle_confirmed = self._raw_state(tail, ratio)
        self._raw = raw

        if raw == PaneState.IDLE:
            self._idle_streak += 1
        else:
            self._idle_streak = 0
        needed = 1 if idle_confirmed else self._settle_ticks

        prev_settled = self._settled
        if raw == PaneState.WORKING:
            new_settled = PaneState.WORKING
        elif raw == PaneState.IDLE and self._idle_streak >= needed:
            new_settled = PaneState.IDLE
        else:
            new_settled = prev_settled  # not enough evidence yet; hold
        self._settled = new_settled

        settled_edge = (
            prev_settled == PaneState.WORKING and new_settled == PaneState.IDLE
        )
        return Observation(
            state=new_settled,
            raw=raw,
            churn=ratio,
            settled_edge=settled_edge,
            content_hash=_hash(tail),
        )


def _hash(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=16).digest()


# --- generic needs-input heuristic -----------------------------------------

# Universal "awaiting an answer" signals, tool-independent. High precision, low
# recall: catches explicit y/n and trailing-question prompts, misses bespoke
# TUI confirmation widgets. NEEDS_INPUT is a cosmetic annotation, so low recall
# is acceptable (the watcher owns the load-bearing "ready" notification).
_NEEDS_INPUT_RE = re.compile(
    r"(y/n|yes/no|\[y/n\]|\[y/n\]|continue\?|proceed\?|overwrite\?)",
    re.IGNORECASE,
)


def detect_needs_input(tail: str) -> bool:
    """Whether a settled tail looks like it is waiting for the user to answer."""
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not lines:
        return False
    last = lines[-1].rstrip()
    if last.endswith("?"):
        return True
    return bool(_NEEDS_INPUT_RE.search("\n".join(lines[-2:])))
