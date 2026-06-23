import threading

from vupai.board import Board, PaneTrack, is_low_information, render_frame
from vupai.panestate import ChurnClassifier, PaneState
from vupai.registry import Pane
from vupai.summarize import Summary

WORK_A = "GENERATED block alpha beta gamma delta epsilon one two three"
WORK_B = "ANOTHER block zeta eta theta iota kappa nine eight seven six"


def _pane(pid, name, *, session="repo", command="claude"):
    return Pane(id=pid, window_id="@1", window="main", index=0, name=name,
                command=command, active=True, session=session)


class _Reg:
    def __init__(self, panes):
        self._panes = panes

    def refresh(self):
        pass

    @property
    def panes(self):
        return self._panes


def _seq(frames_by_pane):
    """capture_fn yielding per-pane frames in order, repeating the last."""
    idx = {}

    def cap(pid):
        frames = frames_by_pane.get(pid, [""])
        i = min(idx.get(pid, 0), len(frames) - 1)
        idx[pid] = idx.get(pid, 0) + 1
        return frames[i]

    return cap


def _board(panes, frames, **kw):
    calls = []

    def fake_summarize(tail):
        calls.append(tail)
        return Summary(f"sum:{tail[:6]}", False, "llm")

    writes = []
    b = Board(
        _Reg(panes),
        self_pane_id=kw.pop("self_pane_id", "%99"),
        capture_fn=_seq(frames),
        summarize_fn=kw.pop("summarize_fn", fake_summarize),
        program_resolver=lambda p: p.command,
        dispatch=lambda fn: fn(),          # synchronous
        writer=writes.append,
        clock=lambda: "14:00",
        now=kw.pop("now", lambda: 10_000.0),
        **kw,
    )
    b._calls = calls
    b._writes = writes
    return b


# --- render_frame (pure) ---------------------------------------------------

def test_render_frame_shows_callsign_state_and_summary():
    t = PaneTrack("nova", "claude", ChurnClassifier(), state=PaneState.WORKING,
                  summary="refactoring auth")
    frame = render_frame([t], "myproj", "14:32")
    assert "vupai board · myproj" in frame
    assert "nova" in frame and "claude" in frame
    assert "working" in frame
    assert "refactoring auth" in frame
    assert "●" in frame


def test_render_frame_needs_input_glyph():
    t = PaneTrack("atlas", "codex", ChurnClassifier(),
                  state=PaneState.NEEDS_INPUT, summary="approve?")
    assert "needs input" in render_frame([t], "s", "00:00")


# --- is_low_information -----------------------------------------------------

def test_low_information_bare_prompt_and_empty():
    assert is_low_information("$ ") is True
    assert is_low_information("") is True
    assert is_low_information("> ") is True


def test_low_information_false_when_content_present():
    assert is_low_information("done: 3 files changed\n$ ") is False


# --- summary triggering -----------------------------------------------------

def test_cold_start_summarizes_idle_pane_once():
    panes = [_pane("%99", "board"), _pane("%1", "nova")]
    frames = {"%1": ["tests running", "tests running"]}  # quiet from the start
    b = _board(panes, frames)
    b.tick()  # baseline
    b.tick()  # settles IDLE -> cold-start summary
    b.tick()  # unchanged -> no second call
    assert len(b._calls) == 1
    assert b._tracks["%1"].summary.startswith("sum:")


def test_working_to_idle_edge_summarizes():
    panes = [_pane("%99", "board"), _pane("%1", "nova")]
    frames = {"%1": ["boot", WORK_A, WORK_A, WORK_A]}
    b = _board(panes, frames)
    for _ in range(4):
        b.tick()
    assert len(b._calls) == 1                       # exactly the edge
    assert b._tracks["%1"].state == PaneState.IDLE


def test_unchanged_tail_is_not_resummarized():
    panes = [_pane("%99", "board"), _pane("%1", "nova")]
    frames = {"%1": ["working area output here now"]}  # same forever
    b = _board(panes, frames)
    for _ in range(6):
        b.tick()
    assert len(b._calls) == 1                       # hash gate after first


def test_min_interval_throttles_second_edge():
    panes = [_pane("%99", "board"), _pane("%1", "nova")]
    frames = {"%1": ["boot", WORK_A, WORK_A, WORK_A, WORK_B, WORK_B, WORK_B]}
    b = _board(panes, frames, now=lambda: 10.0, min_summary_interval=30.0)
    for _ in range(7):
        b.tick()
    # Two genuine WORKING->IDLE edges with different content, but the second is
    # inside the 30s throttle window -> only the first summarizes.
    assert len(b._calls) == 1


def test_low_information_edge_skips_llm():
    panes = [_pane("%99", "board"), _pane("%1", "nova")]
    frames = {"%1": ["boot", WORK_A, "$ ", "$ "]}    # ends at a bare prompt
    b = _board(panes, frames)
    for _ in range(4):
        b.tick()
    assert b._calls == []                            # nothing worth summarizing
    assert b._tracks["%1"].summary == ""


def test_needs_input_is_sticky_until_working_resumes():
    panes = [_pane("%99", "board"), _pane("%1", "nova")]
    frames = {"%1": ["boot", WORK_A, WORK_A, WORK_A, WORK_A, WORK_B]}

    def summ(tail):
        return Summary("approve migration?", True, "llm")

    b = _board(panes, frames, summarize_fn=summ)
    for _ in range(4):
        b.tick()                                     # edge -> NEEDS_INPUT
    assert b._tracks["%1"].state == PaneState.NEEDS_INPUT
    b.tick()                                         # still idle/unchanged
    assert b._tracks["%1"].state == PaneState.NEEDS_INPUT
    b.tick()                                         # WORK_B -> working again
    assert b._tracks["%1"].needs_input is False
    assert b._tracks["%1"].state == PaneState.WORKING


def test_needs_input_unlatches_when_prompt_leaves_without_a_working_frame():
    # On a large tail, replacing just the prompt line is low churn, so the pane
    # never re-enters WORKING; the latch must still clear from the new tail.
    filler = "\n".join(f"log line {i}" for i in range(40))
    work = "\n".join(f"GEN {i} xyzzy" for i in range(40))
    prompt = filler + "\nApply migration? [y/n]"
    answered = filler + "\ndone, all good"
    panes = [_pane("%99", "board"), _pane("%1", "nova")]
    frames = {"%1": ["boot", work, prompt, prompt, prompt, answered, answered]}
    b = _board(panes, frames, summarize_fn=lambda t: Summary("apply?", True, "llm"))
    for _ in range(5):
        b.tick()
    assert b._tracks["%1"].state == PaneState.NEEDS_INPUT   # settled on the prompt
    for _ in range(2):
        b.tick()                                            # prompt gone, low churn
    assert b._tracks["%1"].needs_input is False
    assert b._tracks["%1"].state == PaneState.IDLE


def test_needs_uses_per_tool_markers():
    from vupai.panestate import markers_for
    b = _board([_pane("%99", "board")], {})
    # Claude's multi-line permission menu: the last line is "2. No" (no '?'), so
    # the generic heuristic misses it but the "do you want" marker catches it.
    claude = PaneTrack("nova", "claude", ChurnClassifier(),
                       markers=markers_for("claude"))
    assert b._needs(claude, "Do you want to proceed?\n  1. Yes\n  2. No") is True
    plain = PaneTrack("atlas", "aider", ChurnClassifier())   # no markers
    assert b._needs(plain, "  2. No") is False


# --- scoping / self-exclusion ----------------------------------------------

def test_excludes_self_unnamed_and_other_sessions():
    captured = []
    panes = [
        _pane("%99", "board"),                       # self
        _pane("%1", "nova"),                         # target
        _pane("%2", "%2"),                           # unnamed plain shell
        _pane("%3", "ghost", session="other"),       # different session
    ]
    b = Board(
        _Reg(panes), self_pane_id="%99",
        capture_fn=lambda pid: captured.append(pid) or "x",
        summarize_fn=lambda t: Summary("s", False, "llm"),
        program_resolver=lambda p: p.command,
        dispatch=lambda fn: fn(), writer=lambda f: None,
        clock=lambda: "0", now=lambda: 1.0,
    )
    b.tick()
    assert captured == ["%1"]                         # only the in-session named pane


def test_render_writes_only_on_change():
    panes = [_pane("%99", "board"), _pane("%1", "nova")]
    frames = {"%1": ["steady output line here"]}
    b = _board(panes, frames)
    for _ in range(3):
        b.tick()                                     # reach steady state
    n = len(b._writes)
    b.tick()                                         # identical frame
    assert len(b._writes) == n                       # no redundant write


# --- thread lifecycle -------------------------------------------------------

def test_board_start_and_stop_lifecycle():
    ticked = threading.Event()
    panes = [_pane("%99", "board"), _pane("%1", "nova")]
    b = Board(
        _Reg(panes), self_pane_id="%99",
        capture_fn=lambda pid: ticked.set() or "x",
        summarize_fn=lambda t: Summary("s", False, "llm"),
        program_resolver=lambda p: p.command,
        dispatch=lambda fn: fn(), writer=lambda f: None,
        clock=lambda: "0", now=lambda: 1.0, poll_interval=0.01,
    )
    b.start()
    assert ticked.wait(2.0)
    b.stop()
    assert b._thread is None
