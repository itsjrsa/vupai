from __future__ import annotations

import queue
import threading
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


PANE_LINE = "\t".join(["%1", "@1", "main", "0", "alpha", "node", "1", "repo"])


def make_registry(lines: list[str], focused: str | None) -> PaneRegistry:
    reg = PaneRegistry(lister=lambda: lines, focuser=lambda: focused)
    reg.refresh()
    return reg


def make_daemon(tmp_path, *, transcript: str, lines: list[str], focused: str | None,
                inject_ok: bool = True, filler_filter: bool = True, journal=None):
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
    daemon = Daemon(Config(addressing="keyword", inject_submit_delay=0.0,
                           filler_filter=filler_filter),
                    recorder, transcriber, registry,
                    feedback, route_fn=route_fn, inject_fn=inject_fn,
                    journal=journal,
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
    # hints are the live pane names plus program tokens (biases ASR so "codex"
    # is not heard as "codecs", "opencode" not as "open code")
    assert transcriber.last_hints[0] == "alpha"
    assert {"codex", "opencode"} <= set(transcriber.last_hints)
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
        "\t".join(["%1", "@1", "main", "0", "alpha", "node", "1", "repo"]),
        "\t".join(["%9", "@1", "main", "1", "nova", "node", "0", "repo"]),
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
    monkeypatch.setattr(dmod, "MultiHotkey", FakeHotkey)
    daemon.stop()                         # pre-arm a clean shutdown
    daemon.run()
    assert recorder.is_recording is False
    assert recorder.stopped >= 1


def test_run_warms_and_starts_hotkey(tmp_path, monkeypatch):
    daemon, _, transcriber, _, _, _ = make_daemon(
        tmp_path, transcript="alpha hi", lines=[PANE_LINE], focused="%1")

    started: list[str] = []
    instances: list = []

    class FakeMulti:
        def __init__(self, bindings):
            self.bindings = bindings
            started.append("built")
            instances.append(self)

        def start(self):
            started.append("start")

        def stop(self):
            started.append("stop")

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "MultiHotkey", FakeMulti)
    # Pre-arm shutdown so the consumer loop exits immediately and run() returns.
    daemon.stop()

    daemon.run()
    assert transcriber.warmed == 1          # warmed on the main (run) thread
    assert "start" in started
    assert "stop" in started                # run()'s finally stops the hotkey
    bindings = instances[0].bindings
    keys = [b[0] for b in bindings]
    assert keys == ["alt_r"]                 # keyword mode binds the dictation key
    # the listener received the daemon's real bound callbacks (wiring proof)
    assert bindings[0][1] == daemon.on_press
    assert bindings[0][2] == daemon.on_release


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
    monkeypatch.setattr(dmod, "MultiHotkey", FakeHotkey)
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
    monkeypatch.setattr(dmod, "MultiHotkey", FakeHotkey)
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
    monkeypatch.setattr(dmod, "MultiHotkey", FakeHotkey)
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
    monkeypatch.setattr(dmod, "MultiHotkey", FakeHotkey)
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
    monkeypatch.setattr(dmod, "MultiHotkey", FakeHotkey)
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
    named_line = "\t".join(["%1", "@1", "main", "0", "alpha", "node", "1", "repo"])
    unnamed_line = "\t".join(["%2", "@1", "main", "1", "%2", "zsh", "0", "repo"])
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

    def execute_fn(cmd, registry, config, *, inject_fn, **kwargs):
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
# Spoken talk-back: command acks + the mute/unmute runtime toggle
# ---------------------------------------------------------------------------


def test_create_success_speaks_say_friendly_callsign(tmp_path, monkeypatch):
    # create's result adds info the intent could not (the assigned callsign), so
    # it speaks on success - and the say-friendly `spoken` twin wins. The phrase
    # is spoken verbatim: serialization (not a baked-in silence prefix) keeps it
    # off the heels of the "opening an agent" intent spoken a moment earlier.
    spoken = []
    monkeypatch.setattr("vupai.daemon.speech.speak",
                        lambda text, *, cmd: spoken.append(text))

    def execute_fn(cmd, registry, config, *, inject_fn, **kwargs):
        return CommandResult(True, "created 1 panes: sage", spoken="sage is up")

    d = Daemon(Config(), _Rec(), _Tx("x"), _Reg(_panes()), _Fb(),
               execute_fn=execute_fn)
    d._run_command(Command(kind="create", count=1), {})
    assert spoken == ["sage is up"]


def test_speak_serializes_waiting_on_previous_handle(tmp_path, monkeypatch):
    # The intent ("opening an agent") and the result ("sage is up") are two
    # separate, non-awaited `say` processes; without ordering they talk over each
    # other (the reported overlap). _speak serializes by waiting on the previous
    # handle before spawning the next, so the result starts only once the intent
    # has finished.
    events = []

    class _H:
        def __init__(self, text):
            self.text = text

        def wait(self):
            events.append(("wait", self.text))

    def fake_speak(text, *, cmd):
        events.append(("spawn", text))
        return _H(text)

    monkeypatch.setattr("vupai.daemon.speech.speak", fake_speak)
    d = Daemon(Config(), _Rec(), _Tx("x"), _Reg(_panes()), _Fb())
    d._speak("opening an agent")
    d._speak("sage is up")
    assert events == [
        ("spawn", "opening an agent"),
        ("wait", "opening an agent"),  # result waits for the intent to finish...
        ("spawn", "sage is up"),       # ...before it starts
    ]


def test_single_target_success_is_silent_intent_already_spoke(tmp_path, monkeypatch):
    # focus/close/swap/... say only their immediate intent; a success adds nothing,
    # so _run_command stays quiet (the intent ack covered it).
    spoken = []
    monkeypatch.setattr("vupai.daemon.speech.speak",
                        lambda text, *, cmd: spoken.append(text))

    def execute_fn(cmd, registry, config, *, inject_fn, **kwargs):
        return CommandResult(True, "focused nova", spoken="switched to nova")

    d = Daemon(Config(), _Rec(), _Tx("x"), _Reg(_panes()), _Fb(),
               execute_fn=execute_fn)
    d._run_command(Command(kind="focus", name="nova"), {})
    assert spoken == []


def test_failure_always_speaks_even_for_single_target(tmp_path, monkeypatch):
    spoken = []
    monkeypatch.setattr("vupai.daemon.speech.speak",
                        lambda text, *, cmd: spoken.append(text))

    def execute_fn(cmd, registry, config, *, inject_fn, **kwargs):
        return CommandResult(False, "no pane named sage")

    d = Daemon(Config(), _Rec(), _Tx("x"), _Reg(_panes()), _Fb(),
               execute_fn=execute_fn)
    d._run_command(Command(kind="close", name="sage"), {})
    assert spoken == ["no pane named sage"]


def test_create_success_ack_silent_when_muted(tmp_path, monkeypatch):
    spoken = []
    monkeypatch.setattr("vupai.daemon.speech.speak",
                        lambda text, *, cmd: spoken.append(text))

    def execute_fn(cmd, registry, config, *, inject_fn, **kwargs):
        return CommandResult(True, "created 1 panes: sage", spoken="sage is up")

    d = Daemon(Config(), _Rec(), _Tx("x"), _Reg(_panes()), _Fb(),
               execute_fn=execute_fn)
    d._talkback = False  # runtime mute
    d._run_command(Command(kind="create", count=1), {})
    assert spoken == []


def test_announced_command_speaks_intent_immediately_then_silent_on_success(tmp_path, monkeypatch):
    # A consequential (eyes-off) command announces its intent BEFORE execute; the
    # success adds nothing, so only the one immediate phrase is heard.
    spoken = []
    monkeypatch.setattr("vupai.daemon.speech.speak",
                        lambda text, *, cmd: spoken.append(text))

    def execute_fn(cmd, registry, config, *, inject_fn, **kwargs):
        return CommandResult(True, "sent /clear to nova")

    d = Daemon(Config(), _Rec(), _Tx("clear nova"), _Reg(_panes()), _Fb(),
               execute_fn=execute_fn)
    d._process(_wav(tmp_path), mode="system")
    assert spoken == ["sending clear"]


def test_view_verb_is_silent_on_success_but_speaks_on_failure(tmp_path, monkeypatch):
    # focus/zoom/unzoom/layout/swap show their own on-screen feedback, so a success
    # is voiced by NOTHING (curated talk-back) - but a failure, which you can't see,
    # still speaks.
    spoken = []
    monkeypatch.setattr("vupai.daemon.speech.speak",
                        lambda text, *, cmd: spoken.append(text))

    outcome = {"ok": True, "msg": "focused nova"}

    def execute_fn(cmd, registry, config, *, inject_fn, **kwargs):
        return CommandResult(outcome["ok"], outcome["msg"])

    d = Daemon(Config(), _Rec(), _Tx("focus nova"), _Reg(_panes()), _Fb(),
               execute_fn=execute_fn)
    d._process(_wav(tmp_path), mode="system")
    assert spoken == []  # success: the cursor jump is its own feedback

    outcome.update(ok=False, msg="no pane named nova")
    d._process(_wav(tmp_path), mode="system")
    assert spoken == ["no pane named nova"]  # failure speaks


def test_destructive_speaks_intent_before_popup_then_failure(tmp_path, monkeypatch):
    # close is popup-gated: the intent must be spoken BEFORE the (blocking) confirm,
    # so the user hears "closing sage" at once; the failure trails after execute.
    order = []
    monkeypatch.setattr("vupai.daemon.speech.speak",
                        lambda text, *, cmd: order.append(("speak", text)))

    def confirm_fn(summary, *, timeout, disable_hint):
        order.append(("popup", summary))
        return True

    def execute_fn(cmd, registry, config, *, inject_fn, **kwargs):
        order.append(("execute", cmd.name))
        return CommandResult(False, "no pane named sage")

    d = Daemon(Config(), _Rec(), _Tx("close sage"), _Reg(_panes()), _Fb(),
               execute_fn=execute_fn, confirm_fn=confirm_fn)
    d._process(_wav(tmp_path), mode="system")
    assert order == [("speak", "closing sage"), ("popup", "close sage"),
                     ("execute", "sage"), ("speak", "no pane named sage")]


def test_destructive_cancel_speaks_intent_then_cancelled(tmp_path, monkeypatch):
    spoken = []
    monkeypatch.setattr("vupai.daemon.speech.speak",
                        lambda text, *, cmd: spoken.append(text))
    d = Daemon(Config(), _Rec(), _Tx("close sage"), _Reg(_panes()), _Fb(),
               confirm_fn=lambda *a, **k: False)  # decline the popup
    d._process(_wav(tmp_path), mode="system")
    assert spoken == ["closing sage", "cancelled"]


def test_talkback_seeds_from_config_tts_enabled(tmp_path):
    assert Daemon(Config(tts_enabled=True), _Rec(), _Tx("x"),
                  _Reg(_panes()), _Fb())._talkback is True
    assert Daemon(Config(tts_enabled=False), _Rec(), _Tx("x"),
                  _Reg(_panes()), _Fb())._talkback is False


def test_mute_command_silences_runtime_and_is_itself_silent(tmp_path, monkeypatch):
    spoken = []
    monkeypatch.setattr("vupai.daemon.speech.speak",
                        lambda text, *, cmd: spoken.append(text))
    d = Daemon(Config(), _Rec(), _Tx("mute"), _Reg(_panes()), _Fb())
    assert d._talkback is True
    d._process(_wav(tmp_path), mode="system")
    assert d._talkback is False
    assert spoken == []  # muting takes effect before its own ack would speak


def test_unmute_command_restores_runtime_and_confirms_aloud(tmp_path, monkeypatch):
    spoken = []
    monkeypatch.setattr("vupai.daemon.speech.speak",
                        lambda text, *, cmd: spoken.append(text))
    # Starts muted (persisted default off); the voice command overrides it live.
    d = Daemon(Config(tts_enabled=False), _Rec(), _Tx("unmute"), _Reg(_panes()), _Fb())
    assert d._talkback is False
    d._process(_wav(tmp_path), mode="system")
    assert d._talkback is True
    assert spoken == ["talk back on"]


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

    def execute_fn(cmd, registry, config, *, inject_fn, **kwargs):
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
        "\t".join(["%1", "@1", "main", "0", "alpha", "claude", "1", "repo"]),
        "\t".join(["%9", "@1", "main", "1", "nova", "claude", "0", "repo"]),
    ]


def _confirm_daemon(tmp_path, *, confirm_destructive=True, answer=True,
                    focused="%1"):
    """Daemon wired with a fake execute_fn (records executed commands), the REAL
    parse_command, and a fake confirm_fn (records (summary, timeout) calls and
    returns `answer`), so the confirm gate is exercised end to end."""
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    cfg = Config(confirm_destructive=confirm_destructive)
    registry = make_registry(_close_lines(), focused)
    feedback = FakeFeedback()
    journal = CapturingJournal()
    executed: list = []
    confirms: list = []

    def execute_fn(cmd, reg, config, *, inject_fn, **kwargs):
        executed.append(cmd)
        return CommandResult(True, f"did {cmd.kind} {cmd.name}".strip())

    def confirm_fn(summary, *, timeout, disable_hint=None):
        confirms.append((summary, timeout, disable_hint))
        return answer

    d = Daemon(cfg, FakeRecorder(wav), FakeTranscriber(""), registry, feedback,
               route_fn=lambda *a, **k: Route(
                   pane_id=focused, text="x", matched_name=None,
                   confidence=0.0, fallback=True),
               inject_fn=lambda *a, **k: True, execute_fn=execute_fn,
               confirm_fn=confirm_fn, journal=journal,
               async_fn=lambda fn, *a: fn(*a))
    return d, feedback, journal, executed, confirms, wav


def _say(daemon, wav, text, mode="system"):
    # _process unlinks the source wav after each utterance, so re-create it for
    # the next one (the daemon owns deletion; the recorder would re-create it).
    wav.write_bytes(b"\x00" * 5000)
    daemon._transcriber.transcript = text
    daemon._process(wav, mode)


def test_confirm_disabled_executes_destructive_immediately(tmp_path):
    d, fb, journal, executed, confirms, wav = _confirm_daemon(
        tmp_path, confirm_destructive=False)
    _say(d, wav, "close nova")
    assert [c.kind for c in executed] == ["close"]
    assert confirms == []                       # never prompted


def test_destructive_prompts_and_executes_on_confirm(tmp_path):
    d, fb, journal, executed, confirms, wav = _confirm_daemon(tmp_path, answer=True)
    _say(d, wav, "close nova")
    assert len(confirms) == 1
    summary, timeout, hint = confirms[0]
    assert "close" in summary.lower() and "nova" in summary.lower()
    assert timeout == 8.0                        # confirm_timeout_s threaded through
    assert hint == "confirm_destructive = false in config.toml"
    assert [c.kind for c in executed] == ["close"]
    assert journal.entries[-1].get("confirmed") is True
    assert journal.entries[-1]["outcome"] == "ok"


def test_destructive_declined_does_not_execute(tmp_path):
    d, fb, journal, executed, confirms, wav = _confirm_daemon(tmp_path, answer=False)
    _say(d, wav, "close nova")
    assert len(confirms) == 1                    # was prompted
    assert executed == []                        # but not executed
    assert journal.entries[-1]["outcome"] == "cancelled"
    assert any("cancel" in s.lower() for s in fb.statuses)


def test_non_destructive_command_not_prompted(tmp_path):
    d, fb, journal, executed, confirms, wav = _confirm_daemon(tmp_path)
    _say(d, wav, "focus nova")
    assert confirms == []
    assert [c.kind for c in executed] == ["focus"]


def test_dictation_never_prompts(tmp_path):
    # Dictation injects verbatim and never parses a command, so destructive-
    # sounding dictation can't trigger a confirmation.
    d, fb, journal, executed, confirms, wav = _confirm_daemon(tmp_path)
    _say(d, wav, "close nova", mode="dictation")
    assert confirms == []
    assert executed == []


def test_broadcast_is_gated_by_confirm(tmp_path):
    d, fb, journal, executed, confirms, wav = _confirm_daemon(tmp_path, answer=True)
    _say(d, wav, "everyone deploy the build")
    assert len(confirms) == 1
    assert "broadcast" in confirms[0][0].lower()
    assert [c.kind for c in executed] == ["broadcast"]


def test_large_create_is_gated_by_confirm(tmp_path):
    # count >= confirm_create_threshold (default 8) prompts with an "open N panes"
    # summary and executes once confirmed.
    d, fb, journal, executed, confirms, wav = _confirm_daemon(tmp_path, answer=True)
    _say(d, wav, "create ten panes")
    assert len(confirms) == 1
    assert confirms[0][0] == "open 10 panes"
    assert confirms[0][2] == "raise confirm_create_threshold in config.toml"
    assert [c.kind for c in executed] == ["create"]


def test_large_create_declined_does_not_execute(tmp_path):
    d, fb, journal, executed, confirms, wav = _confirm_daemon(tmp_path, answer=False)
    _say(d, wav, "create ten panes")
    assert len(confirms) == 1
    assert executed == []
    assert journal.entries[-1]["outcome"] == "cancelled"


def test_small_create_not_prompted(tmp_path):
    # Below the threshold a create runs straight through, no popup.
    d, fb, journal, executed, confirms, wav = _confirm_daemon(tmp_path, answer=True)
    _say(d, wav, "create two panes")
    assert confirms == []
    assert [c.kind for c in executed] == ["create"]


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

    def execute_fn(cmd, registry, config, *, inject_fn, **kwargs):
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


def test_button_system_unaddressed_rejected_not_injected(tmp_path):
    # System key + non-command + no name match -> focus fallback. The system key
    # must NOT type it into the focused pane (that's the dictation key's job);
    # it rejects instead.
    daemon, _, _, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="i think we should refactor",
        lines=[PANE_LINE], focused="%1")
    w = tmp_path / "s.wav"
    w.write_bytes(b"\x00" * 5000)
    daemon._process(w, "system")
    assert route_calls                                        # routing was tried
    assert inject_calls == []                                  # nothing typed
    assert feedback.rejects
    assert "dictation" in feedback.rejects[-1][0].lower()


def test_keyword_unaddressed_still_focus_fallback(tmp_path):
    # keyword mode has no command layer and relies on focus fallback by design,
    # so the system-key guard must not touch it.
    daemon, _, _, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="i think we should refactor",
        lines=[PANE_LINE], focused="%1")
    w = tmp_path / "k.wav"
    w.write_bytes(b"\x00" * 5000)
    daemon._process(w, "keyword")
    assert inject_calls == [("%1", "i think we should refactor", 2.0, 0.05)]


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


class _FakeMulti:
    """Capture the bindings a MultiHotkey is built with."""
    last: dict = {}

    def __init__(self, bindings):
        type(self).last = {"bindings": bindings}

    def start(self):
        ...

    def stop(self):
        ...


def test_run_button_multiple_keys_per_action(tmp_path, monkeypatch):
    cfg = Config(addressing="button", hotkey=["alt_r", "f13"],
                 command_hotkey=["cmd_r", "f14"])
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(cfg, FakeRecorder(wav), FakeTranscriber("hi"),
               make_registry([PANE_LINE], "%1"), FakeFeedback())

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "MultiHotkey", _FakeMulti)
    d.stop()
    d.run()
    keys = [b[0] for b in _FakeMulti.last["bindings"]]
    assert set(keys) == {"alt_r", "f13", "cmd_r", "f14"}


def test_run_button_overlapping_keys_falls_back(tmp_path, monkeypatch):
    # A key shared by both actions is ambiguous: fall back to keyword mode over
    # the dictation keys and tell the user why.
    cfg = Config(addressing="button", hotkey=["alt_r", "f13"],
                 command_hotkey=["alt_r"])
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    feedback = FakeFeedback()
    d = Daemon(cfg, FakeRecorder(wav), FakeTranscriber("hi"),
               make_registry([PANE_LINE], "%1"), feedback)

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "MultiHotkey", _FakeMulti)
    d.stop()
    d.run()
    keys = [b[0] for b in _FakeMulti.last["bindings"]]
    assert keys == ["alt_r", "f13"]              # keyword fallback over hotkeys
    assert feedback.errors                        # told the user why


def test_run_keyword_multiple_keys(tmp_path, monkeypatch):
    cfg = Config(addressing="keyword", hotkey=["alt_r", "f13"])
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    d = Daemon(cfg, FakeRecorder(wav), FakeTranscriber("hi"),
               make_registry([PANE_LINE], "%1"), FakeFeedback())

    import vupai.daemon as dmod
    monkeypatch.setattr(dmod, "MultiHotkey", _FakeMulti)
    d.stop()
    d.run()
    keys = [b[0] for b in _FakeMulti.last["bindings"]]
    assert keys == ["alt_r", "f13"]


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


def _make_capturing_daemon(tmp_path, *, transcript, lines, focused, inject_ok=True,
                           filler_filter=True):
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

    daemon = Daemon(Config(addressing="keyword", inject_submit_delay=0.0,
                           filler_filter=filler_filter),
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


# ---------------------------------------------------------------------------
# Task 3: filler filter hook in the daemon
# ---------------------------------------------------------------------------

def test_filler_stripped_before_routing(tmp_path):
    # Leading filler "um" is removed before text reaches route/inject. The raw
    # transcript is preserved in entry["transcript"]; the cleaned text is in
    # entry["filtered_transcript"] and is what routing and injection see.
    journal = CapturingJournal()
    daemon, recorder, transcriber, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="um alpha run the tests", lines=[PANE_LINE], focused="%1",
        journal=journal)
    daemon.on_press()
    _release_and_process(daemon)

    # route received the filler-stripped text (leading "um " removed -> capitalised)
    assert route_calls, "expected at least one route call"
    assert route_calls[0][0] == "Alpha run the tests"
    # inject received the name-stripped remainder
    assert inject_calls == [("%1", "run the tests", 2.0, 0.05)]
    # journal contract: raw transcript preserved; filtered_transcript holds cleaned text
    assert journal.entries, "expected a journal entry"
    entry = journal.entries[-1]
    assert entry["transcript"] == "um alpha run the tests"
    assert entry["filtered_transcript"] == "Alpha run the tests"


def test_filler_filter_disabled_passthrough(tmp_path):
    # With filler_filter=False the raw transcript, including the filler, reaches
    # routing unchanged.
    journal = CapturingJournal()
    daemon, recorder, transcriber, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="um alpha run the tests", lines=[PANE_LINE],
        focused="%1", filler_filter=False, journal=journal)
    daemon.on_press()
    _release_and_process(daemon)

    # route received the unmodified transcript (filler NOT stripped)
    assert route_calls, "expected at least one route call"
    assert route_calls[0][0] == "um alpha run the tests"
    # journal contract: raw transcript always present; filtered_transcript absent
    # when filler_filter is disabled
    assert journal.entries, "expected a journal entry"
    entry = journal.entries[-1]
    assert entry["transcript"] == "um alpha run the tests"
    assert "filtered_transcript" not in entry


def test_all_filler_treated_as_empty(tmp_path):
    # A transcript that collapses entirely to "" after filler stripping must
    # follow the same "didn't catch that" path as a blank transcript.
    journal = CapturingJournal()
    daemon, recorder, transcriber, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="um uh hmm", lines=[PANE_LINE], focused="%1",
        journal=journal)
    daemon.on_press()
    _release_and_process(daemon)

    assert inject_calls == []
    assert route_calls == []
    assert any("catch" in s.lower() for s in feedback.statuses)
    # journal contract: raw transcript present; decision and outcome reflect empty path
    assert journal.entries, "expected a journal entry"
    entry = journal.entries[-1]
    assert entry["transcript"] == "um uh hmm"
    assert entry["decision"] == "empty"
    assert entry["outcome"] == "no_transcript"


# ---------------------------------------------------------------------------
# Tip-rotator lifecycle
# ---------------------------------------------------------------------------

class _FakeRotator:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_daemon_starts_and_stops_tip_rotator(tmp_path):
    # Daemon must start the rotator after warm() and stop it first in teardown.
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    recorder = FakeRecorder(wav)
    transcriber = FakeTranscriber("")
    registry = make_registry([PANE_LINE], "%1")
    feedback = FakeFeedback()
    rot = _FakeRotator()
    daemon = Daemon(Config(addressing="keyword", inject_submit_delay=0.0),
                    recorder, transcriber, registry, feedback,
                    async_fn=lambda fn, *a: fn(*a),
                    tip_rotator=rot)
    daemon.stop()   # queue shutdown sentinel before run() so it exits immediately
    daemon.run()
    assert rot.started is True
    assert rot.stopped is True


def _read_lines():
    return [
        "\t".join(["%1", "@1", "main", "0", "alpha", "node", "1", "repo"]),
        "\t".join(["%9", "@1", "main", "1", "nova", "node", "0", "repo"]),
    ]


def test_read_command_dispatched_off_main_thread_with_own_registry(tmp_path):
    # Read is slow + audible, so the daemon must hand it to async_fn (a worker in
    # production) rather than run it inline, and the worker must use a SEPARATE
    # registry so it never races the main loop's self._registry.refresh().
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    main_reg = make_registry(_read_lines(), "%1")
    read_reg = make_registry(_read_lines(), "%1")
    feedback = FakeFeedback()
    exec_calls: list[tuple] = []
    async_calls: list[tuple] = []

    def execute_fn(cmd, registry, config, *, inject_fn=None, **kwargs):
        exec_calls.append((cmd, registry))
        return CommandResult(True, "nova: refactored auth, tests green")

    def async_fn(fn, *args):
        async_calls.append((fn, args))
        fn(*args)  # run synchronously for a deterministic assertion

    daemon = Daemon(Config(), FakeRecorder(wav), FakeTranscriber("read nova"),
                    main_reg, feedback, execute_fn=execute_fn, async_fn=async_fn,
                    read_registry_factory=lambda: read_reg)
    daemon._process(wav, mode="system")

    assert async_calls, "read must be dispatched, not run inline on the main thread"
    assert exec_calls, "the worker must execute the command"
    cmd, used_reg = exec_calls[0]
    assert cmd.kind == "read" and cmd.name == "nova"
    assert used_reg is read_reg and used_reg is not main_reg
    assert "nova: refactored auth, tests green" in feedback.statuses
    assert not feedback.errors


def test_read_command_failure_is_rejected(tmp_path):
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    main_reg = make_registry(_read_lines(), "%1")
    feedback = FakeFeedback()

    def execute_fn(cmd, registry, config, *, inject_fn=None, **kwargs):
        return CommandResult(False, "no pane named ghost")

    daemon = Daemon(Config(), FakeRecorder(wav), FakeTranscriber("read ghost"),
                    main_reg, feedback, execute_fn=execute_fn,
                    async_fn=lambda fn, *a: fn(*a),
                    read_registry_factory=lambda: make_registry(_read_lines(), "%1"))
    daemon._process(wav, mode="system")

    assert feedback.rejects and "no pane named ghost" in feedback.rejects[0][0]


def test_read_command_journaled_as_dispatched(tmp_path):
    # The worker's outcome lands after this utterance's journal entry is written,
    # so the entry records the dispatch synchronously (the spoken result is logged
    # separately via feedback). A never-fired async_fn proves journaling is sync.
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    recorded: list[dict] = []

    class RecordingJournal:
        def record(self, entry, wav_path):
            recorded.append(dict(entry))

    daemon = Daemon(Config(), FakeRecorder(wav), FakeTranscriber("read nova"),
                    make_registry(_read_lines(), "%1"), FakeFeedback(),
                    execute_fn=lambda *a, **k: CommandResult(True, "nova: ok"),
                    async_fn=lambda fn, *a: None,  # never run the worker
                    journal=RecordingJournal(),
                    read_registry_factory=lambda: make_registry(_read_lines(), "%1"))
    daemon._process(wav, mode="system")

    assert recorded and recorded[0]["decision"] == "command"
    assert recorded[0]["command"] == "read nova"
    assert recorded[0]["outcome"] == "dispatched"


def test_read_reject_hud_pane_comes_from_worker_registry(tmp_path):
    # On failure the HUD overlay target must come from the worker's OWN registry,
    # never self._registry (which the main loop refreshes) - that read would race
    # the refresh. Give the two registries different focus; the worker's must win.
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    main_reg = make_registry(_read_lines(), "%1")    # main thread focuses %1
    worker_reg = make_registry(_read_lines(), "%9")  # worker focuses %9
    feedback = FakeFeedback()
    daemon = Daemon(
        Config(), FakeRecorder(wav), FakeTranscriber("read ghost"), main_reg,
        feedback,
        execute_fn=lambda *a, **k: CommandResult(False, "no pane named ghost"),
        async_fn=lambda fn, *a: fn(*a),
        read_registry_factory=lambda: worker_reg)
    daemon._process(wav, mode="system")

    assert feedback.rejects
    _, pane_id, _ = feedback.rejects[0]
    assert pane_id == "%9"  # worker registry's focus, NOT the main registry's %1


def test_read_worker_swallows_execute_exception(tmp_path):
    # The worker runs on a daemon thread; an exception from execute_fn must not
    # escape (a silent thread death would swallow the outcome). It logs and moves on.
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    feedback = FakeFeedback()

    def boom(cmd, registry, config, *, inject_fn=None, **kwargs):
        raise RuntimeError("execute exploded")

    daemon = Daemon(
        Config(), FakeRecorder(wav), FakeTranscriber("read nova"),
        make_registry(_read_lines(), "%1"), feedback, execute_fn=boom,
        async_fn=lambda fn, *a: fn(*a),
        read_registry_factory=lambda: make_registry(_read_lines(), "%1"))

    daemon._process(wav, mode="system")  # must not raise out of the worker

    # The worker swallowed the failure: no crash, and no result falsely surfaced
    # (only the pre-transcribe "working" indicator, never a read status/reject).
    assert feedback.statuses == ["working"]
    assert not feedback.rejects


# ---------------------------------------------------------------------------
# Task 7: _silence, barge-in on key-down, and stop-command handling
# ---------------------------------------------------------------------------


def test_on_press_silences_inflight_speech(tmp_path):
    # Build a daemon using the standard make_daemon helper with minimal fakes.
    daemon, _, _, _, _, _ = make_daemon(
        tmp_path, transcript="", lines=[PANE_LINE], focused="%1"
    )

    ev = threading.Event()
    daemon._read_cancel = ev

    class _H:
        def __init__(self): self.terminated = False
        def terminate(self): self.terminated = True

    handle = _H()
    daemon._last_ack = handle

    daemon._on_press("system")

    assert ev.is_set(), "read-cancel Event must be set by _silence on key-down"
    assert handle.terminated, "in-flight say handle must be terminated on key-down"


def test_stop_command_silences_without_muting(tmp_path):
    daemon, _, _, _, _, _ = make_daemon(
        tmp_path, transcript="", lines=[PANE_LINE], focused="%1"
    )
    daemon._talkback = True
    ev = threading.Event()
    daemon._read_cancel = ev

    entry: dict = {}
    daemon._handle_stop(Command(kind="stop"), entry)

    assert ev.is_set(), "_handle_stop must set the read-cancel Event"
    assert daemon._talkback is True, "_handle_stop must not touch the persistent mute"
    assert entry["outcome"] == "ok"


def test_run_read_clears_read_cancel_on_completion(tmp_path):
    # After _run_read finishes, _read_cancel must be reset to None so a later
    # barge-in _silence() is a clean no-op (not setting a dead Event).
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    main_reg = make_registry(_read_lines(), "%1")
    feedback = FakeFeedback()
    executed = []

    def execute_fn(cmd, registry, config, *, inject_fn=None, **kwargs):
        executed.append(cmd)
        return CommandResult(True, "alpha: all good")

    daemon = Daemon(
        Config(), FakeRecorder(wav), FakeTranscriber("read alpha"), main_reg,
        feedback, execute_fn=execute_fn,
        async_fn=lambda fn, *a: fn(*a),  # run synchronously
        read_registry_factory=lambda: make_registry(_read_lines(), "%1"))

    cancel = threading.Event()
    daemon._read_cancel = cancel
    daemon._run_read(Command(kind="read", name="alpha"), cancel)

    assert daemon._read_cancel is None, (
        "_read_cancel must be cleared after _run_read finishes")


def test_run_read_does_not_clear_newer_read_cancel(tmp_path):
    # If a second _dispatch_read overwrites _read_cancel with a new Event B
    # while worker A is still in its finally, worker A must NOT clobber B to None.
    wav = tmp_path / "u.wav"
    wav.write_bytes(b"\x00" * 5000)
    main_reg = make_registry(_read_lines(), "%1")
    feedback = FakeFeedback()

    newer_event = threading.Event()

    def execute_fn(cmd, registry, config, *, inject_fn=None, **kwargs):
        # Simulate a concurrent _dispatch_read overwriting _read_cancel mid-worker.
        daemon._read_cancel = newer_event
        return CommandResult(True, "alpha: all good")

    daemon = Daemon(
        Config(), FakeRecorder(wav), FakeTranscriber("read alpha"), main_reg,
        feedback, execute_fn=execute_fn,
        async_fn=lambda fn, *a: fn(*a),
        read_registry_factory=lambda: make_registry(_read_lines(), "%1"))

    cancel_a = threading.Event()
    daemon._read_cancel = cancel_a
    daemon._run_read(Command(kind="read", name="alpha"), cancel_a)

    # Worker A's finally must have detected _read_cancel is not cancel_a and left it.
    assert daemon._read_cancel is newer_event, (
        "worker A must not clobber a newer Event set by a concurrent _dispatch_read")



def test_ssh_kind_in_ack_sets():
    from vupai import daemon
    assert "ssh" in daemon._ANNOUNCE_INTENT
    assert "ssh" in daemon._SPEAK_ON_SUCCESS


def test_summarize_destructive_close_multi():
    from vupai.commands import Command
    from vupai.daemon import _summarize_destructive
    result = _summarize_destructive(Command(kind="close", names=("echo", "sage")), None)
    assert result == "close echo, sage"


def test_summarize_destructive_broadcast_subset():
    from vupai.commands import Command
    from vupai.daemon import _summarize_destructive
    assert _summarize_destructive(
        Command(kind="broadcast", names=("echo", "sage"), text="run tests"), None
    ) == "broadcast to echo, sage: run tests"
    assert _summarize_destructive(
        Command(kind="broadcast", text="run tests"), None
    ) == "broadcast to all agents: run tests"
