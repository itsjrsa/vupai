import signal
import subprocess
from pathlib import Path

import pytest

from voxpane.recorder import Recorder


class FakePopen:
    """Stand-in for subprocess.Popen that records argv and signal/wait calls."""

    instances: list["FakePopen"] = []

    def __init__(self, argv, *args, **kwargs):
        self.argv = argv
        self.args_extra = args
        self.kwargs = kwargs
        self.signals: list[int] = []
        self.waited_timeouts: list[float | None] = []
        FakePopen.instances.append(self)

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def wait(self, timeout=None):
        self.waited_timeouts.append(timeout)
        return 0


@pytest.fixture
def fake_popen(monkeypatch):
    FakePopen.instances = []
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return FakePopen


def test_start_builds_rec_argv_with_sample_rate(fake_popen):
    rec = Recorder(sample_rate=22050)
    rec.start()

    assert len(fake_popen.instances) == 1
    argv = fake_popen.instances[0].argv
    # rec -q -c 1 -r <sr> -b 16 <out.wav>
    assert argv[0] == "rec"
    assert "-q" in argv
    assert argv[argv.index("-c") + 1] == "1"
    assert argv[argv.index("-r") + 1] == "22050"
    assert argv[argv.index("-b") + 1] == "16"
    out = argv[-1]
    assert out.endswith(".wav")


def test_default_sample_rate_is_16000(fake_popen):
    rec = Recorder()
    rec.start()
    argv = fake_popen.instances[0].argv
    assert argv[argv.index("-r") + 1] == "16000"


def test_default_device_passes_no_env(fake_popen):
    # No device pinned -> inherit the parent env (system default input).
    rec = Recorder()
    rec.start()
    assert fake_popen.instances[0].kwargs.get("env") is None


def test_device_sets_audiodev_env(fake_popen):
    rec = Recorder(device="AirPods Pro")
    rec.start()
    env = fake_popen.instances[0].kwargs.get("env")
    assert env is not None
    assert env["AUDIODEV"] == "AirPods Pro"
    # argv is unchanged; the device is selected purely via the environment.
    assert "AirPods Pro" not in fake_popen.instances[0].argv


def test_start_silences_sox_output(fake_popen):
    # sox's device warnings must not leak to the terminal / doctor output.
    rec = Recorder()
    rec.start()
    kw = fake_popen.instances[0].kwargs
    assert kw.get("stdout") == subprocess.DEVNULL
    assert kw.get("stderr") == subprocess.DEVNULL


def test_is_recording_reflects_active_proc(fake_popen):
    rec = Recorder()
    assert rec.is_recording is False
    rec.start()
    assert rec.is_recording is True
    rec.stop()
    assert rec.is_recording is False


def test_stop_sends_sigint_waits_and_returns_wav_path(fake_popen):
    rec = Recorder()
    rec.start()
    proc = fake_popen.instances[0]

    path = rec.stop()

    assert proc.signals == [signal.SIGINT]
    assert proc.waited_timeouts == [5]
    assert isinstance(path, Path)
    assert path.suffix == ".wav"
    # stop() returns the same file Popen was told to write
    assert str(path) == proc.argv[-1]


def test_stop_without_start_raises_runtime_error(fake_popen):
    rec = Recorder()
    with pytest.raises(RuntimeError, match="not recording"):
        rec.stop()


def test_cannot_start_twice(fake_popen):
    rec = Recorder()
    rec.start()
    with pytest.raises(RuntimeError, match="already recording"):
        rec.start()


# ---------------------------------------------------------------------------
# Fix 4: stop() must clear state even if proc.wait() raises TimeoutExpired
# ---------------------------------------------------------------------------

def test_stop_clears_state_on_timeout_expired(monkeypatch):
    """After stop() raises/returns despite TimeoutExpired, recorder is reusable."""
    import subprocess as _subprocess

    class TimeoutPopen:
        def __init__(self, argv, *a, **kw):
            self.argv = argv
            self.signals: list[int] = []

        def send_signal(self, sig: int) -> None:
            self.signals.append(sig)

        def wait(self, timeout=None):
            raise _subprocess.TimeoutExpired(cmd="rec", timeout=timeout)

    monkeypatch.setattr(_subprocess, "Popen", TimeoutPopen)
    rec = Recorder()
    rec.start()
    assert rec.is_recording is True
    # stop() should propagate or absorb the TimeoutExpired but must reset state.
    try:
        rec.stop()
    except _subprocess.TimeoutExpired:
        pass
    # State must be cleared regardless.
    assert rec.is_recording is False
