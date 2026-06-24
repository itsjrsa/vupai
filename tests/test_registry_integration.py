import shutil
import subprocess
import uuid

import pytest

from vupai import tmuxio
from vupai.registry import PaneRegistry

pytestmark = pytest.mark.integration


@pytest.fixture()
def tmux_session(monkeypatch):
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    # Run on an isolated socket (vupai's own-server model), so the test never
    # reads or mutates the user's default tmux server and is unaffected by any
    # leftover global options there (e.g. pane-base-index).
    socket = "vupai-it-sock-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("VTMUX_TMUX_SOCKET", socket)
    session = "vupai-it-" + uuid.uuid4().hex[:8]
    # Detached session so the test never grabs the terminal.
    subprocess.run(
        ["tmux", "-L", socket, "new-session", "-d", "-s", session, "-n", "main"],
        check=True,
    )
    try:
        yield session
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], check=False)


def test_registry_lists_real_pane(tmux_session):
    session = tmux_session
    # Give the single pane a deterministic voice name (the @vupai_name option,
    # which is what the registry reads - pane_title is owned by the running app).
    # Via tmuxio so it targets the same isolated socket as the fixture.
    tmuxio.set_pane_name(f"{session}:main.0", "backend")

    reg = PaneRegistry(lister=tmuxio.list_panes, focuser=tmuxio.focused_pane_id)
    reg.refresh()

    # list_panes is server-wide; find the pane we just titled.
    pane = reg.get("backend")
    assert pane is not None
    assert pane.window == "main"
    assert pane.id.startswith("%")
    assert pane.index == 0
