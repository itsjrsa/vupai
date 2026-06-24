"""Shared pytest fixtures."""
import os

import pytest


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
