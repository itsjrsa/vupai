from vupai.panestate import (
    MARKERS,
    ChurnClassifier,
    Markers,
    PaneState,
    churn,
    classify_state,
    detect_needs_input,
    markers_for,
)


def _drive(clf, frames):
    """Feed frames in order, return the list of Observations."""
    return [clf.observe(f) for f in frames]


# --- marker classifier (back-compat, re-exported by watcher) ---------------

def test_classify_state_markers():
    assert classify_state("x (esc to interrupt)") == PaneState.WORKING
    assert classify_state("> \n? for shortcuts") == PaneState.IDLE
    assert classify_state("$ ls\nfile") == PaneState.UNKNOWN


# --- churn() pure helper ----------------------------------------------------

def test_churn_first_observation_is_zero():
    assert churn(None, "anything") == 0.0


def test_churn_identical_is_zero():
    assert churn("same text", "same text") == 0.0


def test_churn_total_change_is_high():
    assert churn("a" * 50, "z" * 50) > 0.9


# --- ChurnClassifier: tool-agnostic baseline (no markers) ------------------

def test_no_marker_tool_settles_from_churn_and_fires_edge():
    """A tool with zero markers must still reach a WORKING->IDLE settle edge
    purely from content churn. This is the anti-Claude-lock-in guarantee."""
    clf = ChurnClassifier(settle_ticks=2)  # empty markers
    obs = _drive(clf, [
        "starting up",                       # baseline -> IDLE-ish, no edge
        "BIG block of fresh generated text aaaa bbbb cccc dddd",  # WORKING
        "BIG block of fresh generated text aaaa bbbb cccc dddd",  # quiet 1
        "BIG block of fresh generated text aaaa bbbb cccc dddd",  # quiet 2 -> settle
    ])
    assert obs[1].raw == PaneState.WORKING
    assert obs[1].settled_edge is False           # entering WORKING is not an edge
    assert obs[2].settled_edge is False           # one quiet tick: not settled yet
    assert obs[3].state == PaneState.IDLE
    assert obs[3].settled_edge is True            # second quiet tick -> the edge


def test_already_idle_pane_never_fires_edge():
    """A pane quiet from the first observation settles to IDLE but produces no
    WORKING->IDLE edge (there was no working run). Cold-start handles it."""
    clf = ChurnClassifier(settle_ticks=2)
    obs = _drive(clf, ["$ prompt", "$ prompt", "$ prompt"])
    assert all(o.settled_edge is False for o in obs)
    assert obs[-1].state == PaneState.IDLE


def test_sustained_working_does_not_settle():
    clf = ChurnClassifier(settle_ticks=2)
    obs = _drive(clf, [
        "baseline",
        "alpha alpha alpha one two three four five six",
        "zulu zulu zulu nine eight seven six five four",
    ])
    assert obs[1].raw == PaneState.WORKING
    assert obs[2].raw == PaneState.WORKING
    assert all(o.settled_edge is False for o in obs)


def test_hysteresis_dead_band_holds_prior_state():
    clf = ChurnClassifier(active=0.95, idle=0.05, settle_ticks=2)
    _drive(clf, ["x" * 100])             # baseline -> IDLE
    work = clf.observe("z" * 100)        # ~total change -> WORKING
    assert work.raw == PaneState.WORKING
    mid = clf.observe("z" * 80 + "w" * 20)  # ~20% change -> dead-band
    assert 0.05 < mid.churn < 0.95
    assert mid.raw == PaneState.WORKING  # held, did not flap to IDLE


# --- ChurnClassifier: optional marker refinement ---------------------------

def test_working_marker_forces_working_on_quiet_frame():
    clf = ChurnClassifier(markers=Markers(working=("busy",)))
    obs = clf.observe("busy spinner")     # churn 0 on first frame, but marker wins
    assert obs.raw == PaneState.WORKING


def test_idle_marker_settles_in_one_tick():
    clf = ChurnClassifier(markers=Markers(idle=("done",)), settle_ticks=5)
    obs = _drive(clf, [
        "warming",
        "AAAA different generated content here BBBB CCCC",  # WORKING
        "AAAA different generated content here BBBB CCCC\ndone",  # idle marker
    ])
    assert obs[1].raw == PaneState.WORKING
    assert obs[2].settled_edge is True    # one tick despite settle_ticks=5


def test_content_hash_tracks_tail():
    clf = ChurnClassifier()
    a = clf.observe("same")
    b = clf.observe("same")
    c = clf.observe("different")
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


# --- markers_for ------------------------------------------------------------

def test_markers_for_known_and_unknown():
    assert markers_for("claude") is MARKERS["claude"]
    assert markers_for("aider").working == ()      # unknown tool -> empty
    assert markers_for(None).idle == ()


# --- detect_needs_input -----------------------------------------------------

def test_detect_needs_input_question():
    assert detect_needs_input("Working...\nDo you want to proceed?") is True


def test_detect_needs_input_yes_no():
    assert detect_needs_input("Apply this migration? [y/n]") is True


def test_detect_needs_input_negative_on_plain_output():
    assert detect_needs_input("All tests passed.\n$ ") is False
    assert detect_needs_input("") is False
