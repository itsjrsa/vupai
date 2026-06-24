"""Shared pytest fixtures."""
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_tmux_env():
    """vupai's main() exports VTMUX_TMUX_SOCKET (so the detached daemon inherits
    its dedicated socket) and attach() clears $TMUX. Snapshot and restore both so
    those process-global mutations can't leak between tests."""
    saved = {k: os.environ.get(k) for k in ("VTMUX_TMUX_SOCKET", "TMUX")}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
