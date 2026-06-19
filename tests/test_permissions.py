from pathlib import Path

from vtmux import permissions
from vtmux.permissions import PermissionStatus, check_permissions, hints


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
