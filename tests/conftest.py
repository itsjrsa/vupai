"""Shared pytest fixtures."""
import os

import pytest


class _FakeHandle:
    """Stand-in for a `say` Popen handle: the daemon waits on / terminates it."""

    def wait(self, *a, **k):
        return 0

    def terminate(self):
        pass

    def poll(self):
        return 0


@pytest.fixture(autouse=True)
def _mute_tts(monkeypatch):
    """Stop any test from shelling out to the real macOS `say`.

    `Config()` defaults to tts_enabled=True/tts_cmd="say", so a Daemon (or the
    command layer's _default_speaker) built with defaults will spawn `say` for
    every ack unless the test stubs it. Most do; this guarantees the rest stay
    silent. Tests that assert on speech inject their own capture (speak_fn) or
    monkeypatch speech.speak again, which simply overrides this no-op."""
    monkeypatch.setattr("vupai.speech.speak", lambda *a, **k: _FakeHandle())


@pytest.fixture(autouse=True)
def _isolate_tmux_env():
    """vupai's main() exports VTMUX_TMUX_SOCKET (so the detached daemon inherits
    its dedicated socket) and attach() clears $TMUX. Snapshot, then CLEAR both for
    the test body, and restore after.

    Clearing (not just restoring) is what makes the suite deterministic: tmuxio
    reads VTMUX_TMUX_SOCKET live, so a value inherited from the launching shell
    (e.g. running the tests from inside a `vupai` session) would otherwise inject
    `-L vupai` into every argv and break the bare-`tmux` assertions. Tests that
    need either var set it explicitly (monkeypatch.setenv); this guarantees the
    default is unset regardless of the environment the suite runs in."""
    saved = {k: os.environ.get(k) for k in ("VTMUX_TMUX_SOCKET", "TMUX")}
    for key in saved:
        os.environ.pop(key, None)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
