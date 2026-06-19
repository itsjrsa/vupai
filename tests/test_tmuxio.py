import subprocess
from dataclasses import dataclass, field

import pytest

from vtmux import tmuxio


@dataclass
class FakeRun:
    """Records calls to subprocess.run and returns a canned CompletedProcess."""
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    calls: list[dict] = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            args=args, returncode=self.returncode,
            stdout=self.stdout, stderr=self.stderr,
        )


def patch_run(monkeypatch, fake):
    # tmuxio calls subprocess.run; patch the symbol it actually uses.
    monkeypatch.setattr(tmuxio.subprocess, "run", fake)


def test_run_builds_argv_and_returns_stdout(monkeypatch):
    fake = FakeRun(stdout="hello\n")
    patch_run(monkeypatch, fake)
    out = tmuxio.run(["display-message", "-p", "#{pane_id}"])
    assert out == "hello\n"
    call = fake.calls[0]
    assert call["args"] == ["tmux", "display-message", "-p", "#{pane_id}"]
    assert call["kwargs"]["capture_output"] is True
    assert call["kwargs"]["text"] is True
    # No stdin passed -> input must be None.
    assert call["kwargs"].get("input") is None


def test_run_passes_stdin_as_input(monkeypatch):
    fake = FakeRun(stdout="")
    patch_run(monkeypatch, fake)
    tmuxio.run(["load-buffer", "-"], stdin="hello world")
    call = fake.calls[0]
    assert call["args"] == ["tmux", "load-buffer", "-"]
    assert call["kwargs"]["input"] == "hello world"


def test_run_raises_tmuxerror_on_nonzero_with_stderr(monkeypatch):
    fake = FakeRun(returncode=1, stderr="no server running on /tmp/x")
    patch_run(monkeypatch, fake)
    with pytest.raises(tmuxio.TmuxError) as exc:
        tmuxio.run(["has-session"])
    assert "no server running" in str(exc.value)


def test_list_panes_argv_and_splits_lines(monkeypatch):
    fake = FakeRun(stdout="%1\t@1\twin\t0\tname\tzsh\t1\n%2\t@1\twin\t1\tn2\tnode\t0\n")
    patch_run(monkeypatch, fake)
    lines = tmuxio.list_panes()
    assert fake.calls[0]["args"] == ["tmux", "list-panes", "-a", "-F", tmuxio.PANE_FORMAT]
    assert lines == ["%1\t@1\twin\t0\tname\tzsh\t1", "%2\t@1\twin\t1\tn2\tnode\t0"]


def test_list_panes_ignores_blank_lines(monkeypatch):
    fake = FakeRun(stdout="%1\t@1\twin\t0\tname\tzsh\t1\n\n")
    patch_run(monkeypatch, fake)
    assert tmuxio.list_panes() == ["%1\t@1\twin\t0\tname\tzsh\t1"]


def test_focused_pane_id_returns_stripped_id(monkeypatch):
    fake = FakeRun(stdout="%7\n")
    patch_run(monkeypatch, fake)
    assert tmuxio.focused_pane_id() == "%7"
    assert fake.calls[0]["args"] == ["tmux", "display-message", "-p", "#{pane_id}"]


def test_focused_pane_id_returns_none_when_no_server(monkeypatch):
    fake = FakeRun(returncode=1, stderr="no server running")
    patch_run(monkeypatch, fake)
    assert tmuxio.focused_pane_id() is None


def test_load_buffer_argv_and_stdin(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.load_buffer("some text")
    call = fake.calls[0]
    assert call["args"] == ["tmux", "load-buffer", "-"]
    assert call["kwargs"]["input"] == "some text"


def test_paste_buffer_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.paste_buffer("%3")
    assert fake.calls[0]["args"] == ["tmux", "paste-buffer", "-p", "-d", "-t", "%3"]


def test_capture_pane_argv_returns_stdout(monkeypatch):
    fake = FakeRun(stdout="line1\nline2\n")
    patch_run(monkeypatch, fake)
    out = tmuxio.capture_pane("%3")
    assert out == "line1\nline2\n"
    assert fake.calls[0]["args"] == ["tmux", "capture-pane", "-p", "-t", "%3"]


def test_send_enter_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.send_enter("%3")
    assert fake.calls[0]["args"] == ["tmux", "send-keys", "-t", "%3", "Enter"]


def test_set_pane_title_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.set_pane_title("%3", "backend")
    assert fake.calls[0]["args"] == ["tmux", "select-pane", "-t", "%3", "-T", "backend"]


def test_enable_pane_titles_runs_both_set_commands(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.enable_pane_titles()
    assert fake.calls[0]["args"] == ["tmux", "set", "-g", "pane-border-status", "top"]
    assert fake.calls[1]["args"] == ["tmux", "set", "-g", "pane-border-format", "#{pane_title}"]


def test_set_extended_keys_off_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.set_extended_keys_off()
    assert fake.calls[0]["args"] == ["tmux", "set", "-g", "extended-keys", "off"]


def test_display_message_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.display_message("%3", "routed to backend")
    assert fake.calls[0]["args"] == ["tmux", "display-message", "-t", "%3", "routed to backend"]


def test_server_running_true_on_zero_exit(monkeypatch):
    fake = FakeRun(returncode=0)
    patch_run(monkeypatch, fake)
    assert tmuxio.server_running() is True
    assert fake.calls[0]["args"] == ["tmux", "has-session"]


def test_server_running_false_on_error(monkeypatch):
    fake = FakeRun(returncode=1, stderr="no server running")
    patch_run(monkeypatch, fake)
    assert tmuxio.server_running() is False


def test_window_exists_argv_and_match(monkeypatch):
    fake = FakeRun(stdout="main\nvoice\n")
    patch_run(monkeypatch, fake)
    assert tmuxio.window_exists("voice") is True
    assert tmuxio.window_exists("missing") is False
    assert fake.calls[0]["args"] == ["tmux", "list-windows", "-F", "#{window_name}"]


def test_new_window_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.new_window("voice", "python -m vtmux _daemon")
    assert fake.calls[0]["args"] == [
        "tmux", "new-window", "-n", "voice", "python -m vtmux _daemon",
    ]


def test_attach_execs_tmux_attach(monkeypatch):
    captured = {}

    def fake_execvp(file, args):
        captured["file"] = file
        captured["args"] = args

    monkeypatch.setattr(tmuxio.os, "execvp", fake_execvp)
    tmuxio.attach()
    assert captured["file"] == "tmux"
    assert captured["args"] == ["tmux", "attach"]


@pytest.mark.integration
def test_list_panes_roundtrip_real_tmux():
    # Uses a throwaway, isolated tmux server via a private socket name so it
    # cannot touch the user's running tmux.
    import os
    import subprocess as sp

    socket = "vtmux-itest"
    base = ["tmux", "-L", socket]

    def t(args):
        return sp.run(base + args, capture_output=True, text=True)

    # Start a detached session with one window/pane.
    t(["new-session", "-d", "-s", "it", "-n", "w"])
    try:
        created_id = t(["display-message", "-p", "-t", "it", "#{pane_id}"]).stdout.strip()
        assert created_id.startswith("%")
        # Point tmuxio at the same isolated server for this call.
        old = os.environ.get("VTMUX_TMUX_SOCKET")
        os.environ["VTMUX_TMUX_SOCKET"] = socket
        try:
            lines = tmuxio.list_panes()
        finally:
            if old is None:
                os.environ.pop("VTMUX_TMUX_SOCKET", None)
            else:
                os.environ["VTMUX_TMUX_SOCKET"] = old
        ids = [line.split("\t", 1)[0] for line in lines]
        assert created_id in ids
    finally:
        t(["kill-server"])
