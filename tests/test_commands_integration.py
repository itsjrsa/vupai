import os
import subprocess as sp

import pytest

from vupai import tmuxio


@pytest.mark.integration
def test_create_focus_close_roundtrip_real_tmux():
    socket = "vupai-cmd-itest"
    base = ["tmux", "-L", socket]

    def t(args):
        return sp.run(base + args, capture_output=True, text=True)

    t(["new-session", "-d", "-s", "it", "-n", "w"])
    old = os.environ.get("VTMUX_TMUX_SOCKET")
    os.environ["VTMUX_TMUX_SOCKET"] = socket
    try:
        win = t(["display-message", "-p", "-t", "it", "#{window_id}"]).stdout.strip()
        # Create two panes in the window and name them.
        a = tmuxio.split_window(win, "")
        tmuxio.set_pane_name(a, "nova")
        b = tmuxio.split_window(win, "")
        tmuxio.set_pane_name(b, "atlas")
        tmuxio.select_layout(win, "tiled")
        lines = tmuxio.list_panes()
        names = {ln.split("\t")[4] for ln in lines}  # field 5 = @vupai_name
        assert {"nova", "atlas"} <= names
        # Focus, then close one.
        tmuxio.select_pane(a)
        tmuxio.kill_pane(b)
        lines2 = tmuxio.list_panes()
        ids = {ln.split("\t")[0] for ln in lines2}
        assert b not in ids
    finally:
        if old is None:
            os.environ.pop("VTMUX_TMUX_SOCKET", None)
        else:
            os.environ["VTMUX_TMUX_SOCKET"] = old
        t(["kill-server"])
