from __future__ import annotations

from pathlib import Path

import pytest

from vtmux.config import Config
from vtmux.daemon import Daemon
from vtmux.registry import Pane, PaneRegistry
from vtmux.router import Route


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

    def status(self, text: str) -> None:
        self.statuses.append(text)

    def announce(self, route: Route) -> None:
        self.announced.append(route)

    def error(self, text: str) -> None:
        self.errors.append(text)


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

    daemon = Daemon(Config(), recorder, transcriber, registry, feedback,
                    route_fn=route_fn, inject_fn=inject_fn)
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
    daemon.on_release()

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
    daemon.on_release()
    assert inject_calls == []
    assert route_calls == []
    assert any("catch" in s.lower() for s in feedback.statuses)


def test_no_target_reports_error_and_no_inject(tmp_path):
    # focused None and no name match -> route_fn returns pane_id None
    daemon, _, _, feedback, _, inject_calls = make_daemon(
        tmp_path, transcript="run the tests", lines=[PANE_LINE], focused=None)
    daemon.on_press()
    daemon.on_release()
    assert inject_calls == []
    assert feedback.errors and "no target" in feedback.errors[0].lower()


def test_inject_failure_reports_error(tmp_path):
    daemon, _, _, feedback, _, inject_calls = make_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE],
        focused="%1", inject_ok=False)
    daemon.on_press()
    daemon.on_release()
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
    daemon.on_release()

    assert attempts == ["%9", "%1"]  # tried named target, then focused fallback
    assert feedback.announced and feedback.announced[0].pane_id == "%1"
    assert not feedback.errors


def test_empty_wav_reports_permission_hint(tmp_path):
    daemon, recorder, _, feedback, route_calls, inject_calls = make_daemon(
        tmp_path, transcript="alpha run the tests", lines=[PANE_LINE], focused="%1")
    # shrink the wav to a suspiciously empty size
    recorder._wav.write_bytes(b"\x00" * 10)
    daemon.on_press()
    daemon.on_release()
    assert inject_calls == []
    assert route_calls == []
    assert any("microphone" in e.lower() or "permission" in e.lower()
               for e in feedback.errors)


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

    blocked: list[bool] = []

    import vtmux.daemon as dmod
    monkeypatch.setattr(dmod, "Hotkey", FakeHotkey)
    # Replace the blocking wait with an immediate return so the test completes.
    monkeypatch.setattr(dmod.threading.Event, "wait", lambda self, *a, **k: blocked.append(True))

    daemon.run()
    assert transcriber.warmed == 1
    assert "alt_r" in started and "start" in started
    assert blocked  # run() blocked on the event
    # the listener received the daemon's real bound callbacks (wiring proof)
    assert instances[0].on_press == daemon.on_press
    assert instances[0].on_release == daemon.on_release
