from __future__ import annotations

import queue
from pathlib import Path

import pytest

from vupai.commands import Command, CommandResult
from vupai.config import Config
from vupai.daemon import Daemon
from vupai.journal import Journal
from vupai.recorder import MIN_WAV_BYTES
from vupai.registry import Pane, PaneRegistry
from vupai.router import Route


@pytest.fixture(autouse=True)
def _disable_journal(monkeypatch):
    """Daemon defaults to a real Journal under ~/.config; keep these tests from
    writing the user's live journal. Per-module so test_journal is unaffected."""
    monkeypatch.setattr(
        Journal, "from_config",
        classmethod(lambda cls, config, path=None: cls(enabled=False)))


def _release_and_process(daemon) -> None:
    """Production splits the listener-thread release (enqueue) from the main-
    thread consumer (process). Drive both synchronously in unit tests so the
    pipeline (transcribe/route/inject) runs before the assertions."""
    daemon.on_release()
    try:
        job = daemon._jobs.get_nowait()
    except queue.Empty:
        return  # release without a matching start enqueues nothing
    daemon._process(*job)


class FakeRecorder:
    def __init__(self, wav: Path) -> None:
        self._wav = wav
        self._recording = False
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self._recording = True
        self.started += 1

    def stop(self) -> Path:
        self._recording = False
        self.stopped += 1
        return self._wav

    @property
    def is_recording(self) -> bool:
        return self._recording


class FakeTranscriber:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.warmed = 0
        self.last_hints: list[str] | None = None

    def warm(self) -> None:
        self.warmed += 1

    def transcribe(self, wav_path: Path, hints=()) -> str:
        self.last_hints = list(hints)
        return self.transcript


class FakeFeedback:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.errors: list[str] = []
        self.announced: list[Route] = []
        self.confirms: list[str] = []
        self.heards: list[tuple] = []
        self.rejects: list[tuple] = []

    def reserve(self) -> int:
        return 0

    def confirm_prompt(self, summary: str, confirm_word: str = "confirm") -> None:
        self.confirms.append(summary)

    def heard(self, transcript: str, pane_id, *, mode: str = "keyword") -> None:
        self.heards.append((transcript, pane_id, mode))

    def reject(self, reason: str, pane_id, *, candidates: tuple = ()) -> None:
        # A rejection also lands on the error indicator, so record it in errors
        # too (keeps existing error-surface assertions valid) plus the precise
        # reject tuple for pane-targeting assertions.
        self.errors.append(reason)
        self.rejects.append((reason, pane_id, tuple(candidates)))

    def status(self, text: str) -> None:
        self.statuses.append(text)

    def announce(self, route: Route) -> None:
        self.announced.append(route)

    def error(self, text: str, seq: int | None = None) -> None:
        self.errors.append(text)

    def ready(self) -> None:
        self.statuses.append("ready")

    def listening(self, mode: str = "keyword", seq: int | None = None) -> None:
        self.statuses.append(f"listening:{mode}")

    def working(self) -> None:
        self.statuses.append("working")

    def warming(self, downloading: bool = False) -> None:
        self.statuses.append(f"warming:{downloading}")


PANE_LINE = "\t".join(["%1", "@1", "main", "0", "alpha", "node", "1"])


def make_registry(lines: list[str], focused: str | None) -> PaneRegistry:
    reg = PaneRegistry(lister=lambda: lines, focuser=lambda: focused)
    reg.refresh()
    return reg


def make_daemon(tmp_path, *, transcript: str, lines: list[str], focused: str | None,
                inject_ok: bool = True):
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)  # non-trivial size
    recorder = FakeRecorder(wav)
    transcriber = FakeTranscriber(transcript)
    registry = make_registry(lines, focused)
    feedback = FakeFeedback()

    route_calls: list[tuple] = []
    inject_calls: list[tuple] = []

    def route_fn(text, panes, focused_id, *, fuzzy_cutoff=82):
        route_calls.append((text, [p.id for p in panes], focused_id, fuzzy_cutoff))
        # Emulate: strip leading name token "alpha" -> route to %1
        if text.lower().startswith("alpha "):
            return Route(pane_id="%1", text=text.split(" ", 1)[1],
                         matched_name="alpha", confidence=100.0, fallback=False)
        return Route(pane_id=focused_id, text=text, matched_name=None,
                     confidence=0.0, fallback=True)

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05, io=None):
        inject_calls.append((pane_id, text, confirm_timeout, poll_interval))
        return inject_ok

    # These helper-based tests drive the single-key (keyword) path directly via
    # on_press/on_release and the keyword Hotkey; button mode has its own tests.
    daemon = Daemon(Config(addressing="keyword", inject_submit_delay=0.0),
                    recorder, transcriber, registry,
                    feedback, route_fn=route_fn, inject_fn=inject_fn,
                    async_fn=lambda fn, *a: fn(*a))  # run feedback inline for determinism
    return daemon, recorder, transcriber, feedback, route_calls, inject_calls


def test_press_starts_recording_and_announces_listening(tmp_path):
    daemon, recorder, _, feedback, _, _ = make_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE], focused="%1")
    daemon.on_press()
    assert recorder.is_recording is True
    assert recorder.started == 1
    assert "listening" in " ".join(feedback.statuses).lower()


def test_release_routes_and_injects_stripped_text(tmp_path):
    daemon, recorder, transcriber, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE], focused="%1")
    daemon.on_press()
    _release_and_process(daemon)

    assert recorder.stopped == 1
    # hints are the live pane names
    assert transcriber.last_hints == ["alpha"]
    # route received the full transcript and the focused id
    assert route_calls[0][0] == "alpha run the tests"
    assert route_calls[0][2] == "%1"
    # inject got the routed pane and the STRIPPED text + config timeouts
    assert inject_calls == [("%1", "run the tests", 2.0, 0.05)]
    # announced the route
    assert len(feedback.announced) == 1
    assert feedback.announced[0].pane_id == "%1"
    assert not feedback.errors


def test_on_release_recorder_failure_repaints_and_drops(tmp_path, monkeypatch):
    # If recorder.stop() fails for a non-debounce reason, the listener callback
    # must not let the exception escape (it would be swallowed by pynput and the
    # 'listening' indicator painted at press would wedge on). It must repaint an
    # error and enqueue nothing.
    daemon, recorder, _, feedback, _, _ = make_daemon(
        tmp_path, transcript="hi", lines=[PANE_LINE], focused="%1")
    daemon.on_press()

    def boom() -> None:
        raise OSError("wedged recorder")

    monkeypatch.setattr(recorder, "stop", boom)
    daemon.on_release()                  # must NOT raise
    assert daemon._jobs.empty()          # nothing enqueued for processing
    assert feedback.errors               # the stuck listening state was cleared


def test_process_unlinks_source_wav(tmp_path):
    # The recorder writes a temp wav per utterance; the daemon must delete it
    # after journaling, or every push-to-talk leaks a file into $TMPDIR.
    daemon, recorder, _, _, _, _ = make_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE], focused="%1")
    wav = recorder._wav
    assert wav.exists()
    daemon.on_press()
    _release_and_process(daemon)
    assert not wav.exists()


def test_process_unlinks_wav_even_on_no_audio(tmp_path):
    # The tiny/empty-capture path must clean up the source wav too.
    daemon, recorder, _, _, _, _ = make_daemon(
        tmp_path, transcript="ignored", lines=[PANE_LINE], focused="%1")
    recorder._wav.write_bytes(b"\x00" * 10)  # below MIN_WAV_BYTES
    wav = recorder._wav
    daemon.on_press()
    _release_and_process(daemon)
    assert not wav.exists()


def test_blank_transcript_injects_nothing(tmp_path):
    daemon, _, _, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="   ", lines=[PANE_LINE], focused="%1")
    daemon.on_press()
    _release_and_process(daemon)
    assert inject_calls == []
    assert route_calls == []
    assert any("catch" in s.lower() for s in feedback.statuses)


def test_no_target_reports_error_and_no_inject(tmp_path):
    # focused None and no name match -> route_fn returns pane_id None
    daemon, _, _, feedback, _, inject_calls = make_daemon(
        tmp_path, transcript="run the tests", lines=[PANE_LINE], focused=None)
    daemon.on_press()
    _release_and_process(daemon)
    assert inject_calls == []
    assert feedback.errors and "no target" in feedback.errors[0].lower()


def test_inject_failure_reports_error(tmp_path):
    daemon, _, _, feedback, _, inject_calls = make_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE],
        focused="%1", inject_ok=False)
    daemon.on_press()
    _release_and_process(daemon)
    assert inject_calls  # inject was attempted
    assert feedback.errors  # failure surfaced
    assert not feedback.announced


def test_inject_failure_falls_back_to_focused(tmp_path):
    # Routed (named) pane fails to accept; daemon re-resolves and retries the
    # focused pane once, succeeding there.
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    recorder = FakeRecorder(wav)
    transcriber = FakeTranscriber("nova run it")
    lines = [
        "\t".join(["%1", "@1", "main", "0", "alpha", "node", "1"]),
        "\t".join(["%9", "@1", "main", "1", "nova", "node", "0"]),
    ]
    registry = make_registry(lines, "%1")  # focused = %1
    feedback = FakeFeedback()
    attempts: list[str] = []

    def route_fn(text, panes, focused_id, *, fuzzy_cutoff=82):
        return Route(pane_id="%9", text="run it", matched_name="nova",
                     confidence=100.0, fallback=False)

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05,
                  submit_delay=0.0, io=None):
        attempts.append(pane_id)
        return pane_id == "%1"  # only the focused pane accepts

    daemon = Daemon(Config(), recorder, transcriber, registry, feedback,
                    route_fn=route_fn, inject_fn=inject_fn)
    daemon.on_press()
    _release_and_process(daemon)

    assert attempts == ["%9", "%1"]  # tried named target, then focused fallback
    assert feedback.announced and feedback.announced[0].pane_id == "%1"
    assert not feedback.errors


def test_empty_wav_reports_repeatable_dual_cause(tmp_path):
    # A lost/muted device and a denied permission both hit the empty-capture
    # path. The message must name BOTH causes and repeat every time (not blame
    # permission once and go quiet) - a mid-session disconnect is the common case.
    daemon, recorder, _, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE], focused="%1")
    recorder._wav.write_bytes(b"\x00" * 10)  # below MIN_WAV_BYTES

    for _ in range(2):
        daemon.on_press()
        _release_and_process(daemon)

    assert inject_calls == []
    assert route_calls == []
    assert len(feedback.errors) == 2
    assert feedback.errors[0] == feedback.errors[1]  # repeatable, never degraded
    for err in feedback.errors:
        low = err.lower()
        assert "microphone" in low or "permission" in low      # permission cause
        assert any(t in low for t in ("connect", "mute", "device"))  # device cause


def test_empty_wav_message_is_constant(tmp_path):
    # Lock the wording to the module constant so future edits change one place.
    import vupai.daemon as dmod

    daemon, recorder, _, feedback, _, _ = make_daemon(
        tmp_path, transcript="ignored", lines=[PANE_LINE], focused="%1")
    recorder._wav.write_bytes(b"\x00" * 10)
    daemon.on_press()
    _release_and_process(daemon)
    assert feedback.errors == [dmod._NO_AUDIO_MSG]


def test_run_stops_recorder_still_recording_on_shutdown(tmp_path, monkeypatch):
    # `vupai down` mid-capture: the consumer loop exits while a recording is in
    # flight. run()'s teardown must reap the recorder so the sox child isn't
    # orphaned holding the mic.
    daemon, recorder, _, _, _, _ = make_daemon(
        tmp_path, transcript="hi", lines=[PANE_LINE], focused="%1")
    recorder.start()                      # simulate PTT held at shutdown
    assert recorder.is_recording is True

    class FakeHotkey:
        def __init__(self, *a):
            ...

        def start(self):
            ...

        def stop(self):
            ...

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "Hotkey", FakeHotkey)
    daemon.stop()                         # pre-arm a clean shutdown
    daemon.run()
    assert recorder.is_recording is False
    assert recorder.stopped >= 1


def test_run_warms_and_starts_hotkey(tmp_path, monkeypatch):
    daemon, _, transcriber, _, _, _ = make_daemon(
        tmp_path, transcript="alpha hi", lines=[PANE_LINE], focused="%1")

    started: list[str] = []
    instances: list = []

    class FakeHotkey:
        def __init__(self, key_name, on_press, on_release):
            started.append(key_name)
            self.on_press = on_press
            self.on_release = on_release
            instances.append(self)

        def start(self):
            started.append("start")

        def stop(self):
            started.append("stop")

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "Hotkey", FakeHotkey)
    # Pre-arm shutdown so the consumer loop exits immediately and run() returns.
    daemon.stop()

    daemon.run()
    assert transcriber.warmed == 1          # warmed on the main (run) thread
    assert "alt_r" in started and "start" in started
    assert "stop" in started                # run()'s finally stops the hotkey
    # the listener received the daemon's real bound callbacks (wiring proof)
    assert instances[0].on_press == daemon.on_press
    assert instances[0].on_release == daemon.on_release


# ---------------------------------------------------------------------------
# Gap 3: warming indicator painted before the (blocking) warm()
# ---------------------------------------------------------------------------

class _SnapshotTranscriber:
    """Records the feedback.statuses snapshot at the moment warm() is called, so
    a test can prove 'warming' was already painted before the blocking load."""

    def __init__(self, feedback) -> None:
        self._feedback = feedback
        self.warmed = 0
        self.snapshot: list[str] | None = None

    def warm(self) -> None:
        self.warmed += 1
        self.snapshot = list(self._feedback.statuses)

    def transcribe(self, wav, hints=()) -> str:
        return ""


def _run_once_with_fake_hotkey(daemon, monkeypatch) -> None:
    class FakeHotkey:
        def __init__(self, *a):
            ...

        def start(self):
            ...

        def stop(self):
            ...

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "Hotkey", FakeHotkey)
    daemon.stop()
    daemon.run()


def test_run_paints_warming_before_ready(tmp_path, monkeypatch):
    feedback = FakeFeedback()
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(Config(addressing="keyword"), FakeRecorder(wav),
               FakeTranscriber("hi"), make_registry([PANE_LINE], "%1"), feedback)
    _run_once_with_fake_hotkey(d, monkeypatch)
    warming = [i for i, s in enumerate(feedback.statuses) if s.startswith("warming")]
    ready = [i for i, s in enumerate(feedback.statuses) if s == "ready"]
    assert warming and ready and warming[0] < ready[0]


def test_run_warming_painted_before_warm_runs(tmp_path, monkeypatch):
    feedback = FakeFeedback()
    tx = _SnapshotTranscriber(feedback)
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(Config(addressing="keyword"), FakeRecorder(wav), tx,
               make_registry([PANE_LINE], "%1"), feedback)
    _run_once_with_fake_hotkey(d, monkeypatch)
    assert tx.warmed == 1
    assert any(s.startswith("warming") for s in (tx.snapshot or []))


def test_run_warming_downloading_flag_when_model_absent(tmp_path, monkeypatch):
    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "model_cached", lambda mid: False)
    feedback = FakeFeedback()
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(Config(addressing="keyword"), FakeRecorder(wav),
               FakeTranscriber("hi"), make_registry([PANE_LINE], "%1"), feedback)
    _run_once_with_fake_hotkey(d, monkeypatch)
    assert "warming:True" in feedback.statuses


def test_run_warming_not_downloading_when_model_cached(tmp_path, monkeypatch):
    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "model_cached", lambda mid: True)
    feedback = FakeFeedback()
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(Config(addressing="keyword"), FakeRecorder(wav),
               FakeTranscriber("hi"), make_registry([PANE_LINE], "%1"), feedback)
    _run_once_with_fake_hotkey(d, monkeypatch)
    assert "warming:False" in feedback.statuses


# ---------------------------------------------------------------------------
# Gap 4: daemon lifecycle state markers (state_writer)
# ---------------------------------------------------------------------------

def test_run_writes_ready_after_warm_and_stopped_on_exit(tmp_path, monkeypatch):
    # The state marker brackets the model load: 'ready' is written only after
    # warm() returns (the hotkey is about to go live), and 'stopped' on a clean
    # exit (its absence after a dead pid is how `status` detects a crash).
    order: list[str] = []

    class Tx:
        def __init__(self) -> None:
            self.warmed = 0

        def warm(self) -> None:
            self.warmed += 1
            order.append("warm")

        def transcribe(self, wav, hints=()) -> str:
            return ""

    class FakeHotkey:
        def __init__(self, *a):
            ...

        def start(self):
            ...

        def stop(self):
            ...

    phases: list[str] = []

    def state_writer(phase: str) -> None:
        phases.append(phase)
        order.append(f"state:{phase}")

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "Hotkey", FakeHotkey)
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(Config(addressing="keyword"), FakeRecorder(wav), Tx(),
               make_registry([PANE_LINE], "%1"), FakeFeedback(),
               state_writer=state_writer)
    d.stop()
    d.run()
    assert phases == ["ready", "stopped"]
    assert order.index("warm") < order.index("state:ready")


def test_run_starts_and_stops_watcher(tmp_path, monkeypatch):
    class FakeWatcher:
        def __init__(self) -> None:
            self.started = 0
            self.stopped = 0

        def start(self) -> None:
            self.started += 1

        def stop(self) -> None:
            self.stopped += 1

    class FakeHotkey:
        def __init__(self, *a):
            ...

        def start(self):
            ...

        def stop(self):
            ...

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "Hotkey", FakeHotkey)
    fw = FakeWatcher()
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(Config(addressing="keyword"), FakeRecorder(wav),
               FakeTranscriber("hi"), make_registry([PANE_LINE], "%1"),
               FakeFeedback(), watcher=fw)
    d.stop()
    d.run()
    assert fw.started == 1 and fw.stopped == 1


def test_run_without_state_writer_does_not_crash(tmp_path, monkeypatch):
    # The default daemon (no state_writer) must run unchanged.
    class FakeHotkey:
        def __init__(self, *a):
            ...

        def start(self):
            ...

        def stop(self):
            ...

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "Hotkey", FakeHotkey)
    daemon, _, _, _, _, _ = make_daemon(
        tmp_path, transcript="hi", lines=[PANE_LINE], focused="%1")
    daemon.stop()
    daemon.run()  # must not raise


def test_per_utterance_error_writes_no_lifecycle_marker(tmp_path, monkeypatch):
    # state_writer is driven only by run()'s warm/finally, never by a failing
    # _process - so a bad utterance can't emit a spurious lifecycle phase.
    class Tx:
        def warm(self) -> None:
            ...

        def transcribe(self, wav, hints=()) -> str:
            raise RuntimeError("boom")

    class FakeHotkey:
        def __init__(self, *a):
            ...

        def start(self):
            ...

        def stop(self):
            ...

    phases: list[str] = []
    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "Hotkey", FakeHotkey)
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(Config(addressing="keyword"), FakeRecorder(wav), Tx(),
               make_registry([PANE_LINE], "%1"), FakeFeedback(),
               state_writer=lambda phase: phases.append(phase))
    d._jobs.put_nowait((wav, "keyword"))  # one utterance that will raise in _process
    d.stop()                               # then the shutdown sentinel
    d.run()
    assert phases == ["ready", "stopped"]  # no extra marker from the failed utterance


# ---------------------------------------------------------------------------
# Fix 1: unnamed panes must not appear in transcriber hints
# ---------------------------------------------------------------------------

def test_unnamed_pane_excluded_from_asr_hints(tmp_path):
    # A pane whose name == id (pseudo-title set by tmux) must not be included.
    named_line = "\t".join(["%1", "@1", "main", "0", "alpha", "node", "1"])
    unnamed_line = "\t".join(["%2", "@1", "main", "1", "%2", "zsh", "0"])
    daemon, _, transcriber, _, _, _ = make_daemon(
        tmp_path, transcript="alpha hi", lines=[named_line, unnamed_line], focused="%1")
    daemon.on_press()
    _release_and_process(daemon)
    assert transcriber.last_hints is not None
    assert "alpha" in transcriber.last_hints
    assert "%2" not in transcriber.last_hints


# ---------------------------------------------------------------------------
# #2: an ambiguous route surfaces candidates and never injects
# ---------------------------------------------------------------------------

def test_ambiguous_route_surfaces_candidates_without_inject(tmp_path):
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    recorder = FakeRecorder(wav)
    transcriber = FakeTranscriber("nov run it")
    registry = make_registry([PANE_LINE], "%1")
    feedback = FakeFeedback()
    inject_calls: list[str] = []

    def route_fn(text, panes, focused_id, *, fuzzy_cutoff=82):
        return Route(pane_id=None, text=text, matched_name=None,
                     confidence=85.7, fallback=False, candidates=("nova", "novo"))

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05, io=None):
        inject_calls.append(pane_id)
        return True

    daemon = Daemon(Config(), recorder, transcriber, registry, feedback,
                    route_fn=route_fn, inject_fn=inject_fn)
    daemon.on_press()
    _release_and_process(daemon)

    assert inject_calls == []          # ambiguous -> never injected
    assert not feedback.announced
    assert feedback.rejects            # surfaced as a rejection (HUD + indicator)
    reason, pane_id, candidates = feedback.rejects[0]
    assert "nova" in candidates and "novo" in candidates


# ---------------------------------------------------------------------------
# Task 6: command layer interception
# ---------------------------------------------------------------------------


class _Rec:
    def start(self): ...
    def stop(self): ...


class _Tx:
    def __init__(self, text): self._t = text
    def warm(self): ...
    def transcribe(self, wav, hints=None): return self._t


class _Reg:
    def __init__(self, panes): self._p = panes
    def refresh(self): ...
    @property
    def panes(self): return self._p
    def focused(self): return self._p[0] if self._p else None


class _Fb:
    def __init__(self): self.msgs = []
    def reserve(self): return 0
    def status(self, t): self.msgs.append(("status", t))
    def error(self, t, seq=None): self.msgs.append(("error", t))
    def announce(self, r): self.msgs.append(("announce",))
    def ready(self): self.msgs.append(("ready",))
    def listening(self, mode="keyword", seq=None): self.msgs.append(("listening", mode))
    def working(self): self.msgs.append(("working",))
    def warming(self, downloading=False): self.msgs.append(("warming", downloading))

    def confirm_prompt(self, summary, confirm_word="confirm"):
        self.msgs.append(("confirm", summary))

    def heard(self, transcript, pane_id, *, mode="keyword"):
        self.msgs.append(("heard", transcript))

    def reject(self, reason, pane_id, *, candidates=()):
        self.msgs.append(("reject", reason))


def _wav(tmp_path):
    w = tmp_path / "a.wav"
    w.write_bytes(b"\x00" * (MIN_WAV_BYTES + 16))
    return w


def _panes():
    return [Pane(id="%1", window_id="@1", window="main", index=0,
                 name="nova", command="claude", active=True)]


def test_command_path_skips_route_and_inject(tmp_path):
    calls = {"route": 0, "inject": 0}

    def route_fn(text, panes, focused_id, *, fuzzy_cutoff=82):
        calls["route"] += 1
        return Route(pane_id="%1", text=text, matched_name=None,
                     confidence=0.0, fallback=True)

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05):
        calls["inject"] += 1
        return True

    def parse_fn(text, **kw):
        return Command(kind="create", count=2) if text.startswith("computer") else None

    def execute_fn(cmd, registry, config, *, inject_fn):
        return CommandResult(True, "created")

    d = Daemon(Config(), _Rec(), _Tx("computer create two panes"), _Reg(_panes()),
               _Fb(), route_fn=route_fn, inject_fn=inject_fn,
               parse_fn=parse_fn, execute_fn=execute_fn)
    d._process(_wav(tmp_path))
    assert calls == {"route": 0, "inject": 0}


def test_process_signals_working_for_real_capture(tmp_path):
    fb = _Fb()
    d = Daemon(Config(), _Rec(), _Tx("alpha hi"), _Reg(_panes()), fb,
               route_fn=lambda *a, **k: Route(pane_id="%1", text="hi",
                   matched_name="alpha", confidence=100.0, fallback=False),
               inject_fn=lambda *a, **k: True)
    d._process(_wav(tmp_path))
    assert ("working",) in fb.msgs


def test_normal_text_still_routes(tmp_path):
    calls = {"route": 0, "inject": 0}

    def route_fn(text, panes, focused_id, *, fuzzy_cutoff=82):
        calls["route"] += 1
        return Route(pane_id="%1", text=text, matched_name="nova",
                     confidence=100.0, fallback=False)

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05,
                  submit_delay=0.0):
        calls["inject"] += 1
        return True

    def parse_fn(text, **kw):
        return None

    d = Daemon(Config(), _Rec(), _Tx("nova run the tests"), _Reg(_panes()),
               _Fb(), route_fn=route_fn, inject_fn=inject_fn, parse_fn=parse_fn)
    d._process(_wav(tmp_path))
    assert calls == {"route": 1, "inject": 1}


# ---------------------------------------------------------------------------
# Submit review delay (inject_submit_delay): clear-to-cancel before Enter
# ---------------------------------------------------------------------------

def test_route_cancelled_when_inject_returns_none(tmp_path):
    # inject returns None (user cleared the input during the review window):
    # no announce, no error/retry, outcome 'cancelled', delay threaded through.
    cfg = Config(addressing="keyword", inject_submit_delay=0.5)
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    feedback = FakeFeedback()
    seen: list = []

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05,
                  submit_delay=0.0, io=None):
        seen.append((pane_id, submit_delay))
        return None

    d = Daemon(cfg, FakeRecorder(wav), FakeTranscriber("alpha run the tests"),
               make_registry([PANE_LINE], "%1"), feedback,
               route_fn=lambda text, panes, focused_id, *, fuzzy_cutoff=82: Route(
                   pane_id="%1", text="run the tests", matched_name="alpha",
                   confidence=100.0, fallback=False),
               inject_fn=inject_fn)
    d._process(wav, "keyword")

    assert feedback.announced == []
    assert feedback.rejects == []                       # cancel is not an error
    assert seen and seen[0][1] == 0.5                   # configured delay threaded
    assert any("cancel" in s.lower() for s in feedback.statuses)


def test_dictation_cancelled_when_inject_returns_none(tmp_path):
    cfg = Config(inject_submit_delay=0.5)
    wav = tmp_path / "d.wav"
    wav.write_bytes(b"\x00" * 5000)
    feedback = FakeFeedback()

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05,
                  submit_delay=0.0, io=None):
        return None

    d = Daemon(cfg, FakeRecorder(wav), FakeTranscriber("some literal text"),
               make_registry([PANE_LINE], "%1"), feedback, inject_fn=inject_fn)
    d._process(wav, "dictation")

    assert feedback.announced == []
    assert any("cancel" in s.lower() for s in feedback.statuses)


def test_submit_delay_not_passed_to_inject_when_zero(tmp_path):
    # With the delay set to 0, the daemon must NOT pass submit_delay - an
    # inject_fn without that kwarg must still work (no-churn contract).
    called = {"n": 0}

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05, io=None):
        called["n"] += 1
        return True

    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(Config(addressing="keyword", inject_submit_delay=0.0), FakeRecorder(wav),
               FakeTranscriber("alpha hi"), make_registry([PANE_LINE], "%1"),
               FakeFeedback(),
               route_fn=lambda text, panes, focused_id, *, fuzzy_cutoff=82: Route(
                   pane_id="%1", text="hi", matched_name="alpha",
                   confidence=100.0, fallback=False),
               inject_fn=inject_fn)
    d._process(wav, "keyword")  # must not raise on the missing submit_delay kwarg
    assert called["n"] == 1


# ---------------------------------------------------------------------------
# Gap 6: live transcript HUD + rejection surfacing
# ---------------------------------------------------------------------------

def test_process_emits_heard_once_on_success(tmp_path):
    daemon, _, _, feedback, _, _ = make_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE], focused="%1")
    daemon.on_press()
    _release_and_process(daemon)
    assert len(feedback.heards) == 1
    transcript, pane_id, mode = feedback.heards[0]
    assert transcript == "alpha run the tests"
    assert pane_id == "%1"                 # shown on the focused pane
    assert len(feedback.announced) == 1    # announce still fires on success


def test_process_no_heard_on_dictation(tmp_path):
    # Dictation lands in the pane verbatim, so heard would be redundant noise.
    daemon, _, _, feedback, _, _ = make_daemon(
        tmp_path, transcript="some literal text", lines=[PANE_LINE], focused="%1")
    w = tmp_path / "d.wav"
    w.write_bytes(b"\x00" * 5000)
    daemon._process(w, "dictation")
    assert feedback.heards == []


def test_process_no_heard_on_no_audio_or_blank(tmp_path):
    daemon, recorder, _, feedback, _, _ = make_daemon(
        tmp_path, transcript="ignored", lines=[PANE_LINE], focused="%1")
    recorder._wav.write_bytes(b"\x00" * 10)  # below MIN_WAV_BYTES
    daemon.on_press()
    _release_and_process(daemon)
    assert feedback.heards == []             # nothing heard -> no echo

    blank, _, _, feedback2, _, _ = make_daemon(
        tmp_path, transcript="   ", lines=[PANE_LINE], focused="%1")
    blank.on_press()
    _release_and_process(blank)
    assert feedback2.heards == []


def test_process_reject_targets_route_pane_on_inject_failure(tmp_path):
    daemon, _, _, feedback, _, _ = make_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE],
        focused="%1", inject_ok=False)
    daemon.on_press()
    _release_and_process(daemon)
    assert feedback.rejects
    reason, pane_id, _ = feedback.rejects[-1]
    assert "injection failed" in reason.lower()
    assert pane_id == "%1"                    # the routed pane, not None
    assert not feedback.announced


def test_command_unknown_rejects_on_pane(tmp_path):
    # A command that executes but reports failure surfaces as a reject (not a
    # silent status), so the user sees what went wrong.
    def parse_fn(text, **kw):
        return Command(kind="focus", name="ghost")

    def execute_fn(cmd, registry, config, *, inject_fn):
        return CommandResult(False, "no pane named ghost")

    fb = FakeFeedback()
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(Config(), FakeRecorder(wav), FakeTranscriber("focus ghost"),
               make_registry([PANE_LINE], "%1"), fb,
               route_fn=lambda *a, **k: Route(pane_id="%1", text="x",
                   matched_name=None, confidence=0.0, fallback=True),
               inject_fn=lambda *a, **k: True, parse_fn=parse_fn,
               execute_fn=execute_fn)
    d._process(wav, "system")
    assert any("ghost" in r[0] for r in fb.rejects)


# ---------------------------------------------------------------------------
# Gap 2: confirmation for destructive commands
# ---------------------------------------------------------------------------

def _close_lines():
    return [
        "\t".join(["%1", "@1", "main", "0", "alpha", "claude", "1"]),
        "\t".join(["%9", "@1", "main", "1", "nova", "claude", "0"]),
    ]


def _confirm_daemon(tmp_path, *, confirm_destructive=True, confirm_timeout_s=8.0,
                    clock=None, focused="%1"):
    """Daemon wired with a fake execute_fn (records executed commands) and the
    REAL parse_command, so the confirm gate is exercised end to end."""
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    cfg = Config(confirm_destructive=confirm_destructive,
                 confirm_timeout_s=confirm_timeout_s)
    registry = make_registry(_close_lines(), focused)
    feedback = FakeFeedback()
    journal = CapturingJournal()
    executed: list = []

    def execute_fn(cmd, reg, config, *, inject_fn):
        executed.append(cmd)
        return CommandResult(True, f"did {cmd.kind} {cmd.name}".strip())

    clk = clock if clock is not None else (lambda: 0.0)
    d = Daemon(cfg, FakeRecorder(wav), FakeTranscriber(""), registry, feedback,
               route_fn=lambda *a, **k: Route(
                   pane_id=focused, text="x", matched_name=None,
                   confidence=0.0, fallback=True),
               inject_fn=lambda *a, **k: True, execute_fn=execute_fn,
               journal=journal, clock=clk, async_fn=lambda fn, *a: fn(*a))
    return d, feedback, journal, executed, wav


def _say(daemon, wav, text, mode="system"):
    # _process unlinks the source wav after each utterance, so re-create it for
    # the next one (the daemon owns deletion; the recorder would re-create it).
    wav.write_bytes(b"\x00" * 5000)
    daemon._transcriber.transcript = text
    daemon._process(wav, mode)


def test_confirm_disabled_executes_destructive_immediately(tmp_path):
    d, fb, journal, executed, wav = _confirm_daemon(
        tmp_path, confirm_destructive=False)
    _say(d, wav, "close nova")
    assert [c.kind for c in executed] == ["close"]
    assert d._pending is None
    assert fb.confirms == []


def test_destructive_command_arms_pending_and_does_not_execute(tmp_path):
    d, fb, journal, executed, wav = _confirm_daemon(tmp_path)
    _say(d, wav, "close nova")
    assert executed == []
    assert d._pending is not None
    assert any("close" in c.lower() and "nova" in c.lower() for c in fb.confirms)
    assert journal.entries[-1]["decision"] == "confirm_pending"


def test_confirm_word_executes_pending(tmp_path):
    d, fb, journal, executed, wav = _confirm_daemon(tmp_path)
    _say(d, wav, "close nova")
    _say(d, wav, "confirm")
    assert [c.kind for c in executed] == ["close"]
    assert d._pending is None
    assert journal.entries[-1].get("confirmed") is True


def test_cancel_word_drops_pending_without_executing(tmp_path):
    d, fb, journal, executed, wav = _confirm_daemon(tmp_path)
    _say(d, wav, "close nova")
    _say(d, wav, "cancel")
    assert executed == []
    assert d._pending is None
    assert journal.entries[-1]["decision"] == "confirm_cancelled"


def test_non_confirm_utterance_cancels_pending_by_default(tmp_path):
    # Any non-confirm utterance is fail-safe: it drops the pending destructive
    # action AND is itself swallowed as the answer (not executed as its own cmd).
    d, fb, journal, executed, wav = _confirm_daemon(tmp_path)
    _say(d, wav, "close nova")
    _say(d, wav, "focus alpha")
    assert executed == []
    assert d._pending is None


def test_expired_pending_is_dropped_and_new_utterance_processed(tmp_path):
    clk = {"t": 0.0}
    d, fb, journal, executed, wav = _confirm_daemon(
        tmp_path, confirm_timeout_s=8.0, clock=lambda: clk["t"])
    _say(d, wav, "close nova")
    clk["t"] = 9.0  # past the deadline before the next utterance
    _say(d, wav, "confirm")
    # Expired: the held close never ran, and "confirm" was processed fresh
    # (not a command -> route + inject), so nothing destructive executed.
    assert all(c.kind != "close" for c in executed)
    assert d._pending is None


def test_confirm_with_leading_filler_still_confirms(tmp_path):
    d, fb, journal, executed, wav = _confirm_daemon(tmp_path)
    _say(d, wav, "close nova")
    _say(d, wav, "okay confirm")
    assert [c.kind for c in executed] == ["close"]


def test_non_destructive_command_never_arms(tmp_path):
    d, fb, journal, executed, wav = _confirm_daemon(tmp_path)
    _say(d, wav, "focus nova")
    assert d._pending is None
    assert [c.kind for c in executed] == ["focus"]


def test_dictation_path_never_gates(tmp_path):
    # Dictation injects verbatim and never parses a command, so destructive-
    # sounding dictation can't arm a confirmation.
    d, fb, journal, executed, wav = _confirm_daemon(tmp_path)
    _say(d, wav, "close nova", mode="dictation")
    assert d._pending is None
    assert executed == []


def test_pending_then_confirm_journals_two_entries(tmp_path):
    d, fb, journal, executed, wav = _confirm_daemon(tmp_path)
    _say(d, wav, "close nova")
    _say(d, wav, "confirm")
    decisions = [e["decision"] for e in journal.entries]
    assert decisions == ["confirm_pending", "command"]
    assert journal.entries[1].get("confirmed") is True


# ---------------------------------------------------------------------------
# Task 4: button addressing mode
# ---------------------------------------------------------------------------


def test_button_system_command_executes(tmp_path):
    calls = {"route": 0, "inject": 0}
    seen = {}

    def route_fn(text, panes, focused_id, *, fuzzy_cutoff=82):
        calls["route"] += 1
        return Route(pane_id="%1", text=text, matched_name=None,
                     confidence=0.0, fallback=True)

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05):
        calls["inject"] += 1
        return True

    def parse_fn(text, *, broadcast_word, macros, programs, slash_commands,
                 addressing):
        seen["addressing"] = addressing
        return Command(kind="create", count=2)

    def execute_fn(cmd, registry, config, *, inject_fn):
        return CommandResult(True, "created 2 panes")

    d = Daemon(Config(), _Rec(), _Tx("create two panes"), _Reg(_panes()),
               _Fb(), route_fn=route_fn, inject_fn=inject_fn,
               parse_fn=parse_fn, execute_fn=execute_fn)
    d._process(_wav(tmp_path), "system")
    assert seen["addressing"] == "button"          # system -> button addressing
    assert calls == {"route": 0, "inject": 0}      # command executed; no routing


def test_button_system_non_command_routes(tmp_path):
    daemon, _, _, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="alpha hi there", lines=[PANE_LINE], focused="%1")
    w = tmp_path / "s.wav"
    w.write_bytes(b"\x00" * 5000)
    daemon._process(w, "system")
    assert route_calls and route_calls[0][0] == "alpha hi there"
    assert inject_calls == [("%1", "hi there", 2.0, 0.05)]   # name stripped by route


def test_button_dictation_injects_verbatim_to_focused(tmp_path):
    daemon, _, _, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="nova computer create two panes",
        lines=[PANE_LINE], focused="%1")
    w = tmp_path / "d.wav"
    w.write_bytes(b"\x00" * 5000)
    daemon._process(w, "dictation")
    assert route_calls == []                                  # no routing
    assert inject_calls == [("%1", "nova computer create two panes", 2.0, 0.05)]
    assert feedback.announced and feedback.announced[0].pane_id == "%1"


def test_button_dictation_no_focused_errors(tmp_path):
    daemon, _, _, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="hello", lines=[PANE_LINE], focused=None)
    w = tmp_path / "d.wav"
    w.write_bytes(b"\x00" * 5000)
    daemon._process(w, "dictation")
    assert inject_calls == []
    assert feedback.errors and "focus" in feedback.errors[0].lower()


def test_run_button_builds_multihotkey(tmp_path, monkeypatch):
    cfg = Config()
    object.__setattr__(cfg, "addressing", "button")
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(cfg, FakeRecorder(wav), FakeTranscriber("hi"),
               make_registry([PANE_LINE], "%1"), FakeFeedback())

    built = {}

    class FakeMulti:
        def __init__(self, bindings):
            built["bindings"] = bindings

        def start(self):
            built["started"] = True

        def stop(self):
            built["stopped"] = True

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "MultiHotkey", FakeMulti)
    d.stop()
    d.run()
    assert "bindings" in built and len(built["bindings"]) == 2
    keys = [b[0] for b in built["bindings"]]
    assert "alt_r" in keys and "cmd_r" in keys


def test_run_button_duplicate_keys_falls_back(tmp_path, monkeypatch):
    cfg = Config()
    object.__setattr__(cfg, "addressing", "button")
    object.__setattr__(cfg, "command_hotkey", "alt_r")   # same as the dictation key
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    feedback = FakeFeedback()
    d = Daemon(cfg, FakeRecorder(wav), FakeTranscriber("hi"),
               make_registry([PANE_LINE], "%1"), feedback)

    used = []

    class FakeHotkey:
        def __init__(self, key, on_press, on_release):
            used.append(key)

        def start(self):
            ...

        def stop(self):
            ...

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "Hotkey", FakeHotkey)
    d.stop()
    d.run()
    assert used == ["alt_r"]                    # fell back to a single keyword Hotkey
    assert feedback.errors                       # told the user why


class _RecordingJournal:
    def __init__(self):
        self.entries = []

    def record(self, entry, wav=None):
        self.entries.append((entry, wav))


def test_journals_routed_utterance(tmp_path):
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    journal = _RecordingJournal()
    d = Daemon(Config(), FakeRecorder(wav), FakeTranscriber("alpha run tests"),
               make_registry([PANE_LINE], "%1"), FakeFeedback(),
               route_fn=lambda text, panes, focused, **kw: Route(
                   pane_id="%1", text="run tests", matched_name="alpha",
                   confidence=100.0, fallback=False),
               inject_fn=lambda *a, **k: True,
               journal=journal)

    d._process(wav, "keyword")

    assert len(journal.entries) == 1
    entry, kept_wav = journal.entries[0]
    assert entry["transcript"] == "alpha run tests"
    assert entry["decision"] == "route"
    assert entry["outcome"] == "injected"
    assert entry["target_name"] == "alpha"
    assert kept_wav == wav        # real capture is offered to the journal


def test_journals_no_audio_without_wav(tmp_path):
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 4)   # below MIN_WAV_BYTES
    journal = _RecordingJournal()
    d = Daemon(Config(), FakeRecorder(wav), FakeTranscriber("ignored"),
               make_registry([PANE_LINE], "%1"), FakeFeedback(),
               journal=journal)

    d._process(wav, "keyword")

    entry, kept_wav = journal.entries[0]
    assert entry["outcome"] == "no_audio"
    assert kept_wav is None        # empty capture is never retained


# ---------------------------------------------------------------------------
# Task 2: journal entry enrichment
# ---------------------------------------------------------------------------

class CapturingJournal:
    """Stand-in Journal that records entries for assertions."""
    def __init__(self):
        self.entries = []

    def record(self, entry, wav=None):
        self.entries.append(dict(entry))


def _make_capturing_daemon(tmp_path, *, transcript, lines, focused, inject_ok=True):
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    recorder = FakeRecorder(wav)
    transcriber = FakeTranscriber(transcript)
    registry = make_registry(lines, focused)
    feedback = FakeFeedback()
    journal = CapturingJournal()

    def route_fn(text, panes, focused_id, *, fuzzy_cutoff=82):
        if text.lower().startswith("alpha "):
            return Route(pane_id="%1", text=text.split(" ", 1)[1],
                         matched_name="alpha", confidence=100.0, fallback=False,
                         match_method="exact")
        return Route(pane_id=focused_id, text=text, matched_name=None,
                     confidence=0.0, fallback=True, match_method="focus_fallback")

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05, io=None):
        return inject_ok

    daemon = Daemon(Config(addressing="keyword", inject_submit_delay=0.0),
                    recorder, transcriber, registry,
                    feedback, route_fn=route_fn, inject_fn=inject_fn, journal=journal,
                    async_fn=lambda fn, *a: fn(*a))
    return daemon, journal


def test_journal_entry_carries_common_fields(tmp_path):
    daemon, journal = _make_capturing_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE], focused="%1")
    daemon.on_press()
    _release_and_process(daemon)

    assert len(journal.entries) == 1
    e = journal.entries[0]
    assert e["v"] == 1
    # millisecond-precision timestamp: an ISO string with a fractional second.
    assert "." in e["ts"]
    assert e["model_id"] == Config().model_id
    assert isinstance(e["transcribe_ms"], int)


def test_journal_route_entry_carries_routing_fields(tmp_path):
    daemon, journal = _make_capturing_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE], focused="%1")
    daemon.on_press()
    _release_and_process(daemon)

    e = journal.entries[0]
    assert e["decision"] == "route"
    assert e["confidence"] == 100.0
    assert e["match_method"] == "exact"
    assert e["available_names"] == ["alpha"]
    assert isinstance(e["inject_ms"], int)


def test_journal_no_audio_entry_has_version_but_no_timing(tmp_path):
    daemon, journal = _make_capturing_daemon(
        tmp_path, transcript="alpha run it", lines=[PANE_LINE], focused="%1")
    daemon._recorder._wav.write_bytes(b"\x00" * 10)  # below MIN_WAV_BYTES
    daemon.on_press()
    _release_and_process(daemon)

    e = journal.entries[0]
    assert e["decision"] == "no_audio"
    assert e["v"] == 1
    assert "transcribe_ms" not in e  # transcription never ran
