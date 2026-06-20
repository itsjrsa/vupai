from pathlib import Path

import pytest

from voxpane import permissions
from voxpane.permissions import (
    PermissionStatus,
    TerminalApp,
    check_permissions,
    fixes,
    hints,
    open_settings_pane,
    terminal_app,
)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # Keep the mic-probe's capture wait from actually sleeping in unit tests.
    monkeypatch.setattr(permissions.time, "sleep", lambda *_a, **_k: None)


class _FakeRecorder:
    """Recorder stand-in: start() writes a wav of a controllable size, stop() returns it."""

    def __init__(self, wav_path: Path, size_bytes: int) -> None:
        self._wav_path = wav_path
        self._size_bytes = size_bytes
        self._recording = False

    def start(self) -> None:
        self._recording = True
        # Simulate sox writing a wav; size encodes "did the mic capture audio?".
        self._wav_path.write_bytes(b"\x00" * self._size_bytes)

    def stop(self) -> Path:
        self._recording = False
        return self._wav_path

    @property
    def is_recording(self) -> bool:
        return self._recording


def _factory(wav_path: Path, size_bytes: int):
    return lambda: _FakeRecorder(wav_path, size_bytes)


def test_microphone_true_when_wav_is_non_trivial(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "_probe_listener", lambda: True)
    big_wav = tmp_path / "mic_ok.wav"
    status = check_permissions(recorder_factory=_factory(big_wav, 50_000))
    assert status.microphone is True


def test_microphone_false_when_wav_is_empty_or_tiny(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "_probe_listener", lambda: True)
    tiny_wav = tmp_path / "mic_silent.wav"
    status = check_permissions(recorder_factory=_factory(tiny_wav, 100))
    assert status.microphone is False


def test_microphone_false_when_recorder_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "_probe_listener", lambda: True)

    class _Boom:
        def start(self) -> None:
            raise RuntimeError("rec failed to spawn")

        def stop(self) -> Path:  # pragma: no cover - never reached
            raise AssertionError

        @property
        def is_recording(self) -> bool:
            return False

    status = check_permissions(recorder_factory=lambda: _Boom())
    assert status.microphone is False


def test_listener_probe_success_sets_both_gates_true(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "_probe_listener", lambda: True)
    big_wav = tmp_path / "ok.wav"
    status = check_permissions(recorder_factory=_factory(big_wav, 50_000))
    assert status.input_monitoring is True
    assert status.accessibility is True


def test_listener_probe_failure_sets_both_gates_false(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "_probe_listener", lambda: False)
    big_wav = tmp_path / "ok.wav"
    status = check_permissions(recorder_factory=_factory(big_wav, 50_000))
    assert status.input_monitoring is False
    assert status.accessibility is False


def test_microphone_probe_records_before_stopping(tmp_path, monkeypatch):
    # Regression: the probe must let sox capture audio BETWEEN start and stop,
    # else the wav is header-only and reads as "no mic" even when granted.
    events: list[str] = []

    class _OrderRecorder:
        def __init__(self, wav: Path) -> None:
            self._wav = wav

        def start(self) -> None:
            events.append("start")
            self._wav.write_bytes(b"\x00" * 50_000)

        def stop(self) -> Path:
            events.append("stop")
            return self._wav

    monkeypatch.setattr(permissions, "_probe_listener", lambda: True)
    monkeypatch.setattr(permissions.time, "sleep",
                        lambda s, **_k: events.append(f"sleep:{s}"))
    wav = tmp_path / "m.wav"
    status = check_permissions(recorder_factory=lambda: _OrderRecorder(wav))

    assert status.microphone is True
    assert events[0] == "start" and events[-1] == "stop"
    sleeps = [e for e in events[1:-1] if e.startswith("sleep:")]
    assert sleeps, "probe must wait between start and stop so audio is captured"
    assert float(sleeps[0].split(":")[1]) >= 0.2


def test_hints_all_true_is_empty():
    status = PermissionStatus(microphone=True, input_monitoring=True, accessibility=True)
    assert hints(status) == []


def test_hints_lists_the_right_panes_for_each_false():
    status = PermissionStatus(microphone=False, input_monitoring=False, accessibility=False)
    out = hints(status)
    joined = "\n".join(out)
    assert "Privacy & Security > Microphone" in joined
    assert "Privacy & Security > Input Monitoring" in joined
    assert "Privacy & Security > Accessibility" in joined
    assert len(out) == 3


def test_hints_only_includes_failing_fields():
    status = PermissionStatus(microphone=True, input_monitoring=False, accessibility=True)
    out = hints(status)
    joined = "\n".join(out)
    assert "Input Monitoring" in joined
    assert "Microphone" not in joined
    assert "Accessibility" not in joined
    assert len(out) == 1


def test_hints_names_app_and_includes_deep_link_when_app_given():
    status = PermissionStatus(microphone=False, input_monitoring=True, accessibility=True)
    out = hints(status, app=TerminalApp("Ghostty", "com.mitchellh.ghostty"))
    assert len(out) == 1
    assert "Ghostty" in out[0]
    assert "x-apple.systempreferences:" in out[0]
    assert "Privacy_Microphone" in out[0]


# --- terminal_app -----------------------------------------------------------

def test_terminal_app_known_term_program():
    app = terminal_app({"TERM_PROGRAM": "Apple_Terminal"})
    assert app.name == "Terminal"
    assert app.bundle_id == "com.apple.Terminal"


def test_terminal_app_unknown_falls_back_to_bundle_id():
    app = terminal_app({"TERM_PROGRAM": "Weird", "__CFBundleIdentifier": "com.weird.app"})
    assert app.name == "Weird"
    assert app.bundle_id == "com.weird.app"


def test_terminal_app_empty_env_is_generic_placeholder():
    app = terminal_app({})
    assert app.name == "your terminal app"
    assert app.bundle_id is None


# --- fixes ------------------------------------------------------------------

def test_fixes_only_for_failing_permissions():
    status = PermissionStatus(microphone=False, input_monitoring=True, accessibility=False)
    fs = fixes(status)
    assert [f.field for f in fs] == ["microphone", "accessibility"]
    assert all(f.url.startswith("x-apple.systempreferences:") for f in fs)
    assert fs[0].reset_service == "Microphone"


def test_fixes_empty_when_all_granted():
    status = PermissionStatus(microphone=True, input_monitoring=True, accessibility=True)
    assert fixes(status) == []


# --- open_settings_pane -----------------------------------------------------

def test_open_settings_pane_invokes_open_with_url():
    calls: list[list[str]] = []
    ok = open_settings_pane("x-apple.systempreferences:foo",
                            runner=lambda argv, **k: calls.append(argv))
    assert ok is True
    assert calls == [["open", "x-apple.systempreferences:foo"]]


def test_open_settings_pane_swallows_errors():
    def boom(*_a, **_k):
        raise OSError("open missing")

    assert open_settings_pane("x-apple.systempreferences:foo", runner=boom) is False


def test_missing_tools_reports_absent_binaries(monkeypatch):
    # rec (sox) absent, tmux present -> report the sox package only.
    present = {"tmux"}
    monkeypatch.setattr(permissions.shutil, "which",
                        lambda name: ("/usr/bin/" + name) if name in present else None)
    assert permissions.missing_tools() == ["sox"]


def test_missing_tools_empty_when_all_present(monkeypatch):
    monkeypatch.setattr(permissions.shutil, "which", lambda name: "/usr/bin/" + name)
    assert permissions.missing_tools() == []
