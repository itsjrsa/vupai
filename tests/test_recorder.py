import signal
import subprocess
import tempfile
from pathlib import Path

import pytest

from vupai import recorder as recorder_mod
from vupai.recorder import Recorder, _is_vupai_rec, reap_orphan_recordings


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
# stop() must force-kill and reap sox if SIGINT doesn't take within the timeout,
# never orphan the child, and clear state without raising.
# ---------------------------------------------------------------------------

def test_stop_kills_and_reaps_when_sigint_times_out(monkeypatch):
    """If sox ignores SIGINT (wait(timeout) raises), stop() escalates to kill()
    then reaps the child so no `rec` is orphaned, and returns without raising."""
    import subprocess as _subprocess

    created: list = []

    class TimeoutPopen:
        def __init__(self, argv, *a, **kw):
            self.argv = argv
            self.signals: list[int] = []
            self.killed = False
            created.append(self)

        def send_signal(self, sig: int) -> None:
            self.signals.append(sig)

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout=None):
            if timeout is not None:        # the SIGINT wait(timeout=5)
                raise _subprocess.TimeoutExpired(cmd="rec", timeout=timeout)
            return 0                        # the post-kill reap

    monkeypatch.setattr(_subprocess, "Popen", TimeoutPopen)
    rec = Recorder()
    rec.start()
    assert rec.is_recording is True

    path = rec.stop()                       # must NOT raise

    proc = created[0]
    assert proc.signals == [signal.SIGINT]  # tried the graceful stop first
    assert proc.killed is True              # then escalated to a force-kill
    assert rec.is_recording is False        # state cleared
    assert isinstance(path, Path)


# ---------------------------------------------------------------------------
# reap_orphan_recordings: kill stray `rec` children a crashed daemon left behind
# without touching a user's own sox/rec session.
# ---------------------------------------------------------------------------

def _vupai_cmd(rate: int = 16000) -> str:
    wav = f"{tempfile.gettempdir()}/tmpABCDEF.wav"
    return f"rec -q -c 1 -r {rate} -b 16 {wav}"


def test_is_vupai_rec_matches_our_signature():
    assert _is_vupai_rec(_vupai_cmd()) is True
    assert _is_vupai_rec(_vupai_cmd(rate=22050)) is True
    # absolute path to the rec binary still matches on basename
    assert _is_vupai_rec(f"/usr/local/bin/{_vupai_cmd()}") is True


def test_is_vupai_rec_rejects_foreign_sessions():
    tmp = tempfile.gettempdir()
    # user's own stereo/8-bit/loud rec - not our fingerprint
    assert _is_vupai_rec(f"rec -c 2 -b 24 {tmp}/song.wav") is False
    # right flags but writing outside the temp dir (user-chosen path)
    assert _is_vupai_rec("rec -q -c 1 -r 16000 -b 16 /Users/me/voice.wav") is False
    # a different binary entirely
    assert _is_vupai_rec(f"sox -q -c 1 -b 16 {tmp}/x.wav") is False
    assert _is_vupai_rec("") is False


def test_reap_kills_matching_processes_and_skips_self(monkeypatch):
    me = 4242
    ps_out = (
        f"{me} {_vupai_cmd()}\n"        # our own pid - must be skipped
        "111 " + _vupai_cmd() + "\n"     # stray vupai rec - kill
        "222 rec -c 2 -b 24 /tmp/song.wav\n"  # foreign - leave alone
    )

    class _PS:
        stdout = ps_out

    monkeypatch.setattr(recorder_mod.os, "getpid", lambda: me)
    monkeypatch.setattr(recorder_mod.subprocess, "run", lambda *a, **k: _PS())
    monkeypatch.setattr(recorder_mod.time, "sleep", lambda *_: None)

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(recorder_mod.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))

    n = reap_orphan_recordings()

    assert n == 1
    assert (111, signal.SIGINT) in killed
    assert (111, signal.SIGKILL) in killed
    assert all(pid != me and pid != 222 for pid, _ in killed)


def test_reap_returns_zero_when_nothing_matches(monkeypatch):
    class _PS:
        stdout = "999 rec -c 2 -b 24 /tmp/song.wav\n"

    monkeypatch.setattr(recorder_mod.subprocess, "run", lambda *a, **k: _PS())
    monkeypatch.setattr(recorder_mod.os, "kill",
                        lambda *a: pytest.fail("must not kill foreign rec"))
    assert reap_orphan_recordings() == 0


def test_reap_survives_ps_failure(monkeypatch):
    def _boom(*a, **k):
        raise OSError("ps missing")

    monkeypatch.setattr(recorder_mod.subprocess, "run", _boom)
    assert reap_orphan_recordings() == 0  # best-effort, no raise
