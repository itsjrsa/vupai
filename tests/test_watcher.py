import threading

from vupai.registry import PaneRegistry
from vupai.tmuxio import TmuxError
from vupai.watcher import PaneState, PaneWatcher, classify_state

NAMED = "\t".join(["%1", "@1", "main", "0", "nova", "claude", "1"])
NAMED2 = "\t".join(["%9", "@1", "main", "2", "sage", "claude", "0"])
UNNAMED = "\t".join(["%2", "@1", "main", "1", "%2", "zsh", "0"])


def _reg(lines):
    # The watcher refreshes the registry itself inside tick().
    return PaneRegistry(lister=lambda: lines, focuser=lambda: None)


def _seq_capture(frames):
    """capture_fn that yields `frames` in order, repeating the last forever."""
    box = {"n": 0}

    def cap(pane_id):
        i = min(box["n"], len(frames) - 1)
        box["n"] += 1
        return frames[i]

    return cap


# --- classify_state (the fragile heuristic, isolated) ----------------------

def test_classify_working_on_interrupt_hint():
    assert classify_state("thinking... (esc to interrupt)") == PaneState.WORKING


def test_classify_idle_on_prompt_footer():
    assert classify_state("│ > │\n? for shortcuts") == PaneState.IDLE


def test_classify_unknown_on_plain_shell():
    assert classify_state("$ ls -la\nfile.txt\n") == PaneState.UNKNOWN


def test_classify_unknown_on_empty():
    assert classify_state("") == PaneState.UNKNOWN


# --- PaneWatcher.tick transitions ------------------------------------------

def test_tick_notifies_on_working_to_idle():
    notos = []
    w = PaneWatcher(_reg([NAMED]),
                    capture_fn=_seq_capture(["x (esc to interrupt)",
                                             "? for shortcuts"]),
                    notifier=lambda title, msg: notos.append((title, msg)))
    w.tick()  # WORKING: first observation, baseline only
    w.tick()  # IDLE: the busy->idle edge -> notify
    assert len(notos) == 1
    assert "nova" in notos[0][1]


def test_tick_does_not_notify_while_state_stable():
    notos = []
    w = PaneWatcher(_reg([NAMED]),
                    capture_fn=_seq_capture(["esc to interrupt",
                                             "? for shortcuts",
                                             "? for shortcuts"]),
                    notifier=lambda *a: notos.append(a))
    w.tick()
    w.tick()
    w.tick()
    assert len(notos) == 1  # only the single busy->idle edge fired


def test_tick_skips_unnamed_panes():
    captured = []
    w = PaneWatcher(_reg([UNNAMED]),
                    capture_fn=lambda pid: captured.append(pid) or "",
                    notifier=lambda *a: None)
    w.tick()
    assert captured == []  # an unnamed (plain-shell) pane is never captured


def test_tick_first_observation_and_unknown_never_notify():
    notos = []
    w = PaneWatcher(_reg([NAMED]),
                    capture_fn=_seq_capture(["? for shortcuts",          # idle baseline
                                             "esc to interrupt",         # working
                                             "$ shell noise"]),          # unknown
                    notifier=lambda *a: notos.append(a))
    w.tick()
    w.tick()
    w.tick()
    assert notos == []  # no busy->idle edge anywhere


def test_chime_invoked_only_when_chimer_set():
    chimes = []
    w = PaneWatcher(_reg([NAMED]),
                    capture_fn=_seq_capture(["esc to interrupt",
                                             "? for shortcuts"]),
                    notifier=lambda *a: None,
                    chimer=lambda: chimes.append(1))
    w.tick()
    w.tick()
    assert chimes == [1]


def test_no_chimer_does_not_error():
    w = PaneWatcher(_reg([NAMED]),
                    capture_fn=_seq_capture(["esc to interrupt",
                                             "? for shortcuts"]),
                    notifier=lambda *a: None, chimer=None)
    w.tick()
    w.tick()  # no raise


def test_debounce_suppresses_rapid_renotify():
    notos = []
    w = PaneWatcher(_reg([NAMED]),
                    capture_fn=_seq_capture(["esc to interrupt",
                                             "? for shortcuts",
                                             "esc to interrupt",
                                             "? for shortcuts"]),
                    notifier=lambda *a: notos.append(a),
                    clock=lambda: 0.0, debounce=10.0)
    w.tick()
    w.tick()
    w.tick()
    w.tick()
    # Two busy->idle edges, but the second is inside the debounce window.
    assert len(notos) == 1


def test_notifier_exception_swallowed():
    def boom(*a):
        raise RuntimeError("notify failed")

    w = PaneWatcher(_reg([NAMED]),
                    capture_fn=_seq_capture(["esc to interrupt",
                                             "? for shortcuts"]),
                    notifier=boom)
    w.tick()
    w.tick()  # must not raise out of tick()


def test_capture_failure_skips_pane_but_processes_others():
    notos = []

    def cap_working(pid):
        if pid == "%1":
            raise TmuxError("pane vanished")
        return "esc to interrupt"

    def cap_idle(pid):
        if pid == "%1":
            raise TmuxError("pane vanished")
        return "? for shortcuts"

    w = PaneWatcher(_reg([NAMED, NAMED2]), capture_fn=cap_working,
                    notifier=lambda title, msg: notos.append(msg))
    w.tick()                       # %1 errors (skipped), %9 working baseline
    w._capture_fn = cap_idle
    w.tick()                       # %9 busy->idle -> notify
    assert len(notos) == 1 and "sage" in notos[0]


# --- thread lifecycle -------------------------------------------------------

def test_watcher_start_and_stop_lifecycle():
    ticked = threading.Event()
    w = PaneWatcher(_reg([NAMED]),
                    capture_fn=lambda pid: ticked.set() or "",
                    notifier=lambda *a: None, poll_interval=0.01)
    w.start()
    assert ticked.wait(2.0)        # the loop ran at least one tick
    w.stop()
    assert w._thread is None        # joined and cleared
