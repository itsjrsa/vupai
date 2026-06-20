from __future__ import annotations

import queue
from pathlib import Path

import pytest

from voxpane.commands import CommandResult
from voxpane.config import Config
from voxpane.daemon import Daemon
from voxpane.journal import Journal
from voxpane.recorder import MIN_WAV_BYTES
from voxpane.registry import Pane, PaneRegistry
from voxpane.router import Route


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

    def reserve(self) -> int:
        return 0

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
    daemon = Daemon(Config(addressing="keyword"), recorder, transcriber, registry,
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

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05, io=None):
        attempts.append(pane_id)
        return pane_id == "%1"  # only the focused pane accepts

    daemon = Daemon(Config(), recorder, transcriber, registry, feedback,
                    route_fn=route_fn, inject_fn=inject_fn)
    daemon.on_press()
    _release_and_process(daemon)

    assert attempts == ["%9", "%1"]  # tried named target, then focused fallback
    assert feedback.announced and feedback.announced[0].pane_id == "%1"
    assert not feedback.errors


def test_empty_wav_reports_permission_hint(tmp_path):
    daemon, recorder, _, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE], focused="%1")
    # shrink the wav to a suspiciously empty size
    recorder._wav.write_bytes(b"\x00" * 10)

    # First cycle: must mention microphone/permission (one-time hint).
    daemon.on_press()
    _release_and_process(daemon)
    assert inject_calls == []
    assert route_calls == []
    assert len(feedback.errors) == 1
    first_error = feedback.errors[0].lower()
    assert "microphone" in first_error or "permission" in first_error

    # Second cycle: wav is still tiny; must emit the GENERIC message only.
    daemon.on_press()
    _release_and_process(daemon)
    assert inject_calls == []
    assert route_calls == []
    assert len(feedback.errors) == 2
    second_error = feedback.errors[1].lower()
    assert "no audio captured" in second_error
    assert "microphone" not in second_error
    assert "permission" not in second_error


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

    import voxpane.daemon as dmod
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
    assert feedback.errors
    msg = feedback.errors[0].lower()
    assert "nova" in msg and "novo" in msg


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

    def command_fn(text, registry, config, *, inject_fn, addressing="keyword"):
        return CommandResult(True, "created") if text.startswith("computer") else None

    d = Daemon(Config(), _Rec(), _Tx("computer create two panes"), _Reg(_panes()),
               _Fb(), route_fn=route_fn, inject_fn=inject_fn, command_fn=command_fn)
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

    def inject_fn(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05):
        calls["inject"] += 1
        return True

    def command_fn(text, registry, config, *, inject_fn, addressing="keyword"):
        return None

    d = Daemon(Config(), _Rec(), _Tx("nova run the tests"), _Reg(_panes()),
               _Fb(), route_fn=route_fn, inject_fn=inject_fn, command_fn=command_fn)
    d._process(_wav(tmp_path))
    assert calls == {"route": 1, "inject": 1}


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

    def command_fn(text, registry, config, *, inject_fn, addressing="keyword"):
        seen["addressing"] = addressing
        return CommandResult(True, "created 2 panes")

    d = Daemon(Config(), _Rec(), _Tx("create two panes"), _Reg(_panes()),
               _Fb(), route_fn=route_fn, inject_fn=inject_fn, command_fn=command_fn)
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

    import voxpane.daemon as dmod
    monkeypatch.setattr(dmod, "MultiHotkey", FakeMulti)
    d.stop()
    d.run()
    assert "bindings" in built and len(built["bindings"]) == 2
    keys = [b[0] for b in built["bindings"]]
    assert "alt_r" in keys and "alt_l" in keys


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

    import voxpane.daemon as dmod
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
