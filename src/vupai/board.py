"""Supervision board: per-pane agent summaries in a dedicated tmux pane.

The board runs as the foreground program of its own tmux pane (see the `_board`
CLI subcommand) and renders, for every named agent pane in its session, a short
summary of that pane's main conclusion or pending action. It is a structural
twin of watcher.PaneWatcher - its OWN PaneRegistry, an Event-interruptible poll
loop, exception-swallowing ticks - differing only in its output sink: it prints
a frame to its own stdout instead of firing a notification.

Token cost (the primary design constraint) is bounded by summarizing only on a
settled WORKING->IDLE edge, gated by a content hash (unchanged tail -> no call),
a per-pane min-interval throttle, an in-flight guard, a bounded scrollback tail,
a low-information pre-filter, and a global concurrency cap. See
docs/supervision-board-plan.md.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from vupai import summarize as summarize_mod
from vupai import tmuxio
from vupai.panestate import (
    ChurnClassifier,
    Markers,
    PaneState,
    detect_needs_input,
    markers_for,
)

logger = logging.getLogger(__name__)

_DEFAULT_SUMMARIZER = "claude -p --model claude-haiku-4-5"

# Internal tuning (kept out of the TOML surface; constructor-overridable for tests).
_CAPTURE_LINES = 40
_TAIL_BYTES = 6000
_MAX_CONCURRENT = 2

_GLYPHS = {
    PaneState.WORKING: "●",      # filled circle
    PaneState.IDLE: "◌",         # dotted circle
    PaneState.NEEDS_INPUT: "◆",  # diamond
    PaneState.UNKNOWN: "·",      # middle dot
}
_STATE_LABEL = {
    PaneState.WORKING: "working",
    PaneState.IDLE: "idle",
    PaneState.NEEDS_INPUT: "needs input",
    PaneState.UNKNOWN: "...",
}

_BARE_PROMPT_RE = re.compile(r"^[\$%#>❯❱]\s*$")


def is_low_information(tail: str) -> bool:
    """Whether a settled tail has nothing worth spending an LLM call on.

    Conservative on purpose: only an empty pane or a lone bare shell prompt
    qualifies, so a real conclusion is never silently filtered out.
    """
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    if not lines:
        return True
    return len(lines) == 1 and bool(_BARE_PROMPT_RE.match(lines[0]))


@dataclass
class PaneTrack:
    """Per-pane state held across ticks by the board."""
    callsign: str
    program: str
    classifier: ChurnClassifier
    markers: Markers = field(default_factory=Markers)
    state: PaneState = PaneState.UNKNOWN
    summary: str = ""
    needs_input: bool = False
    last_summary_hash: bytes = b""
    last_summary_at: float = 0.0
    inflight: bool = False
    cold_start_pending: bool = True


def render_frame(tracks, session: str, clock: str) -> str:
    """Build the board frame (pure: no IO). `tracks` is render-ordered."""
    header = f" vupai board · {session or '-'}"
    out = [f"{header}{clock:>{max(1, 50 - len(header))}}", ""]
    for t in tracks:
        glyph = _GLYPHS.get(t.state, _GLYPHS[PaneState.UNKNOWN])
        label = _STATE_LABEL.get(t.state, "...")
        out.append(f" {glyph} {t.callsign:<8} {t.program:<8} {label}")
        # No blank spacer between panes: keep the board compact so every pane
        # fits in a short board pane (a taller frame scrolls the top pane off).
        out.append(f"     {t.summary}" if t.summary else "")
    return "\n".join(out)


def _default_writer(frame: str) -> None:
    sys.stdout.write("\033[2J\033[H" + frame + "\n")
    sys.stdout.flush()


class Board:
    """Polls named panes in its session and renders per-pane summaries.

    Collaborators are injected so the unit suite drives ticks deterministically
    (no thread, no tmux, no subprocess): pass `capture_fn`, `summarize_fn`, and a
    synchronous `dispatch` to make summaries land inline.
    """

    def __init__(self, registry, *, self_pane_id: str | None = None,
                 capture_fn=tmuxio.capture_pane, summarize_fn=None,
                 program_resolver=None, writer=_default_writer,
                 dispatch=None, clock=lambda: time.strftime("%H:%M"),
                 now=time.monotonic, poll_interval: float = 2.0,
                 min_summary_interval: float = 30.0,
                 summarizer_cmd: str = _DEFAULT_SUMMARIZER,
                 summary_timeout: float = 20.0,
                 capture_lines: int = _CAPTURE_LINES, tail_bytes: int = _TAIL_BYTES,
                 max_concurrent: int = _MAX_CONCURRENT) -> None:
        self._registry = registry
        self._self_pane_id = (
            self_pane_id if self_pane_id is not None
            else os.environ.get("TMUX_PANE"))
        self._capture_fn = capture_fn
        self._summarize_fn = summarize_fn or (
            lambda tail: summarize_mod.summarize(
                tail, cmd=summarizer_cmd, timeout=summary_timeout))
        self._program_resolver = program_resolver or _default_program
        self._writer = writer
        self._sem = threading.Semaphore(max(1, max_concurrent))
        self._dispatch = dispatch or self._spawn
        self._clock = clock
        self._now = now
        self._poll_interval = poll_interval
        self._min_interval = min_summary_interval
        self._capture_lines = capture_lines
        self._tail_bytes = tail_bytes
        self._tracks: dict[str, PaneTrack] = {}
        self._session = ""
        self._last_frame: str | None = None
        # Guards self._tracks (dict structure) and the frame compare/write, so a
        # summary worker rendering never iterates the dict while the poll thread
        # inserts/pops panes (a dict-changed-size-during-iteration RuntimeError).
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- one poll cycle -----------------------------------------------------

    def tick(self) -> None:
        """One synchronous poll cycle. Safe to call directly in tests."""
        try:
            self._registry.refresh()
        except Exception:
            logger.debug("board registry refresh failed", exc_info=True)
            return
        targets = self._target_panes()
        seen: set[str] = set()
        now = self._now()
        for pane in targets:
            seen.add(pane.id)
            track = self._track_for(pane)
            try:
                text = self._capture_fn(pane.id)
            except Exception:
                continue  # pane vanished mid-tick; skip, keep going
            tail = self._bounded(text)
            obs = track.classifier.observe(tail)
            if obs.raw == PaneState.WORKING:
                track.needs_input = False
            elif (track.needs_input and not track.inflight
                  and obs.content_hash != track.last_summary_hash):
                # The summarized prompt has scrolled away but the pane never
                # re-entered WORKING (a low-churn answer): re-derive the latch
                # from the current tail so a now-clean prompt drops the diamond.
                # Unchanged tail keeps the summary's verdict (don't second-guess).
                track.needs_input = self._needs(track, tail)
            track.state = PaneState.NEEDS_INPUT if track.needs_input else obs.state
            self._maybe_summarize(track, tail, obs, now)
        # Drop vanished panes so a recreated id starts fresh.
        with self._lock:
            for pid in list(self._tracks):
                if pid not in seen:
                    self._tracks.pop(pid, None)
        self._render()

    def _target_panes(self):
        panes = self._registry.panes
        own = next((p for p in panes if p.id == self._self_pane_id), None)
        if own is not None:
            self._session = own.session
        session = self._session
        out = []
        for p in panes:
            if self._self_pane_id is None or p.id == self._self_pane_id:
                continue          # no self id -> watch nothing; never watch self
            if p.name == p.id:
                continue          # unnamed plain-shell pane
            if session and p.session != session:
                continue          # other sessions' panes (scoped to ours)
            out.append(p)
        return out

    def _track_for(self, pane) -> PaneTrack:
        with self._lock:
            track = self._tracks.get(pane.id)
            if track is None:
                program = self._program_resolver(pane)
                markers = markers_for(program)
                track = PaneTrack(
                    callsign=pane.name, program=program, markers=markers,
                    classifier=ChurnClassifier(markers=markers))
                self._tracks[pane.id] = track
            else:
                track.callsign = pane.name  # follow renames
            return track

    def _needs(self, track: PaneTrack, tail: str) -> bool:
        """Generic needs-input heuristic plus this tool's optional markers."""
        if detect_needs_input(tail):
            return True
        low = tail.lower()
        return any(m in low for m in track.markers.needs_input)

    def _bounded(self, text: str) -> str:
        lines = text.splitlines()
        if self._capture_lines:
            lines = lines[-self._capture_lines:]
        tail = "\n".join(lines)
        if self._tail_bytes:
            raw = tail.encode("utf-8", "replace")
            if len(raw) > self._tail_bytes:
                tail = raw[-self._tail_bytes:].decode("utf-8", "replace")
        return tail

    # --- summary gating -----------------------------------------------------

    def _maybe_summarize(self, track: PaneTrack, tail: str, obs, now: float) -> None:
        trigger = obs.settled_edge
        if track.cold_start_pending:
            # Summarize a pane found already-idle at board open, once.
            if obs.raw == PaneState.WORKING:
                track.cold_start_pending = False  # active; a later settle is a real edge
            elif obs.state == PaneState.IDLE:
                trigger = True
                track.cold_start_pending = False
        if not trigger or track.inflight:
            return
        if obs.content_hash == track.last_summary_hash:
            return  # tail unchanged since the last summary
        if track.last_summary_at and now - track.last_summary_at < self._min_interval:
            return  # per-pane throttle floor (first summary is never throttled)
        if is_low_information(tail):
            # Synchronous, always succeeds -> commit the gate inline.
            track.last_summary_hash = obs.content_hash
            track.last_summary_at = now
            track.summary = ""
            track.needs_input = self._needs(track, tail)
            if track.needs_input:
                track.state = PaneState.NEEDS_INPUT
            return  # nothing worth an LLM call
        # LLM path: the in-flight guard covers re-entry; the hash/throttle gate is
        # committed only on success (in _run_summary) so a failed summary retries
        # on the next edge instead of being suppressed for min_interval.
        track.inflight = True
        self._dispatch(lambda: self._run_summary(track, tail, obs.content_hash, now))

    def _run_summary(self, track: PaneTrack, tail: str,
                     content_hash: bytes, at: float) -> None:
        try:
            res = self._summarize_fn(tail)
        except Exception:
            logger.debug("board summarizer failed", exc_info=True)
            res = None
        if res is not None:
            track.last_summary_hash = content_hash  # commit the gate on success
            track.last_summary_at = at
            track.summary = res.text
            # Don't paint a stale NEEDS over a pane the poll thread already moved
            # back to WORKING while this summary was in flight.
            if track.state != PaneState.WORKING:
                track.needs_input = res.needs_input
                if res.needs_input:
                    track.state = PaneState.NEEDS_INPUT
                elif track.state == PaneState.NEEDS_INPUT:
                    track.state = PaneState.IDLE
        track.inflight = False
        self._render()

    def _spawn(self, fn) -> None:
        """Default dispatch: a daemon worker bounded by the concurrency cap."""
        def runner():
            with self._sem:
                fn()
        threading.Thread(target=runner, daemon=True).start()

    # --- rendering ----------------------------------------------------------

    def _render(self) -> None:
        with self._lock:
            tracks = sorted(self._tracks.values(), key=lambda t: t.callsign.lower())
            frame = render_frame(tracks, self._session, self._clock())
            if frame == self._last_frame:
                return
            self._last_frame = frame
            try:
                self._writer(frame)
            except Exception:
                logger.debug("board write failed", exc_info=True)

    # --- thread lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def run(self) -> None:
        """Run the poll loop in the CURRENT thread until stop() is called.

        Used by the `_board` in-pane process, whose whole job is the board.
        stop() (from a signal handler) sets the Event and wakes the wait.
        """
        self._stop.clear()
        self._loop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            if self._stop.wait(self._poll_interval):
                break

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None


def _default_program(pane) -> str:
    """Program label for marker selection: @vupai_program, else pane_current_command."""
    try:
        prog = tmuxio.pane_program(pane.id)
    except Exception:
        prog = ""
    return prog or pane.command


@dataclass
class PaneStatus:
    """One pane's board-style status: callsign, program, state, one-line summary."""
    callsign: str
    program: str
    state: PaneState
    summary: str
    needs_input: bool


def _bound(text: str, capture_lines: int, tail_bytes: int) -> str:
    """Last N lines then last M UTF-8 bytes (the standalone twin of Board._bounded)."""
    lines = text.splitlines()
    if capture_lines:
        lines = lines[-capture_lines:]
    tail = "\n".join(lines)
    if tail_bytes:
        raw = tail.encode("utf-8", "replace")
        if len(raw) > tail_bytes:
            tail = raw[-tail_bytes:].decode("utf-8", "replace")
    return tail


def collect_statuses(panes, *, summarize_fn, capture_fn=tmuxio.capture_pane,
                     program_resolver=_default_program, settle_pause: float = 0.6,
                     sleep=time.sleep, capture_lines: int = _CAPTURE_LINES,
                     tail_bytes: int = _TAIL_BYTES,
                     max_workers: int = _MAX_CONCURRENT) -> list[PaneStatus]:
    """Snapshot each pane's state + a one-line summary on demand, no board pane.

    Mirrors Board.tick without its polling thread: two captures `settle_pause`
    apart feed a fresh ChurnClassifier (working vs idle), refined by needs-input;
    the board's one-line `summarize_fn` then runs per pane (concurrently). This
    lets the spoken `read board` command build the same data the visual board
    shows even when no board pane is open. Capture failures drop that pane.
    """
    targets = list(panes)
    first: dict[str, str | None] = {}
    for p in targets:
        try:
            first[p.id] = _bound(capture_fn(p.id), capture_lines, tail_bytes)
        except Exception:
            first[p.id] = None
    if targets:
        sleep(settle_pause)  # one shared settle window, then re-sample for churn

    pending = []  # (pane, program, state, needs, tail)
    for p in targets:
        program = program_resolver(p)
        markers = markers_for(program)
        clf = ChurnClassifier(markers=markers)
        prev = first.get(p.id)
        if prev is not None:
            clf.observe(prev)
        try:
            tail = _bound(capture_fn(p.id), capture_lines, tail_bytes)
        except Exception:
            continue  # pane vanished between samples; skip it
        obs = clf.observe(tail)
        low = tail.lower()
        needs = detect_needs_input(tail) or any(m in low for m in markers.needs_input)
        state = PaneState.NEEDS_INPUT if needs else obs.state
        pending.append((p, program, state, needs, tail))

    def _summ(tail: str):
        try:
            return summarize_fn(tail)
        except Exception:
            logger.debug("read-board summarizer failed", exc_info=True)
            return None

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
            summaries = list(ex.map(lambda item: _summ(item[4]), pending))
    else:
        summaries = []

    out: list[PaneStatus] = []
    for (p, program, state, needs, _tail), summary in zip(pending, summaries):
        text = summary.text if summary is not None else ""
        # A summarizer that detected a pending question latches NEEDS even if the
        # single-frame heuristic above missed it (e.g. the prompt scrolled).
        needs = needs or (summary.needs_input if summary is not None else False)
        st = PaneState.NEEDS_INPUT if needs else state
        out.append(PaneStatus(p.name, program, st, text, needs))
    return out


def speak_statuses(statuses) -> str:
    """One spoken digest of every agent: callsign, program, state, then summary."""
    if not statuses:
        return "No agents to report."
    n = len(statuses)
    parts = [f"{n} agent{'s' if n != 1 else ''} on the board."]
    for s in statuses:
        label = _STATE_LABEL.get(s.state, "unknown")
        head = f"{s.callsign}, {s.program}, {label}" if s.program else f"{s.callsign}, {label}"
        clause = f"{head}: {s.summary}" if s.summary else head
        # Terminate each clause so agents don't run together when a one-line
        # summary lacks end punctuation ("...the board atlas, claude...").
        parts.append(clause if clause.endswith((".", "!", "?")) else clause + ".")
    return " ".join(parts)


def _self_cmd() -> str:
    """How to re-invoke this CLI from a tmux pane (absolute interpreter)."""
    return f"{sys.executable} -m vupai"


def open_board(target_pane: str, session: str, *, io=tmuxio,
               self_cmd: str | None = None) -> tuple[bool, str]:
    """Split a supervision-board pane off `target_pane`. Returns (opened, message).

    One board per session: if `session` already has a board pane (the
    @vupai_board tag), focus it and return (False, ...) rather than splitting a
    second one (two boards would summarize each other's frames). Shared by the
    `vupai board` CLI command and the spoken "board" verb.
    """
    existing = io.find_board_pane(session) if session else None
    if existing is not None:
        io.select_pane(existing)
        return False, "board already open in this session"
    inner = f"{self_cmd or _self_cmd()} _board"
    pane_id = io.split_window(target_pane, inner, horizontal=True, size="40%")
    io.set_pane_name(pane_id, "board")  # cosmetic; the board excludes itself by id
    io.mark_board_pane(pane_id)
    return True, "opened board"
