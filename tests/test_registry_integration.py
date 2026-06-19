import shutil
import subprocess
import uuid

import pytest

from voxpane import tmuxio
from voxpane.registry import PaneRegistry

pytestmark = pytest.mark.integration


@pytest.fixture()
def tmux_session():
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    session = "voxpane-it-" + uuid.uuid4().hex[:8]
    # Detached session so the test never grabs the terminal.
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-n", "main"],
        check=True,
    )
    try:
        yield session
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False)


def test_registry_lists_real_pane(tmux_session):
    session = tmux_session
    # Give the single pane a deterministic voice name (the @voxpane_name option,
    # which is what the registry reads - pane_title is owned by the running app).
    subprocess.run(
        ["tmux", "set", "-p", "-t", f"{session}:main.0", "@voxpane_name", "backend"],
        check=True,
    )

    reg = PaneRegistry(lister=tmuxio.list_panes, focuser=tmuxio.focused_pane_id)
    reg.refresh()

    # list_panes is server-wide; find the pane we just titled.
    pane = reg.get("backend")
    assert pane is not None
    assert pane.window == "main"
    assert pane.id.startswith("%")
    assert pane.index == 0
