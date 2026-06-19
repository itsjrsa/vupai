import sys
import types
from pathlib import Path

import pytest

from vtmux import cli


class FakeTmux:
    """In-memory stand-in for the tmuxio module."""

    def __init__(self, *, server=True, windows=None, focused="%1"):
        self._server = server
        self.windows = set(windows or [])
        self._focused = focused
        self.calls: list[tuple] = []

    def server_running(self) -> bool:
        return self._server

    def enable_pane_titles(self) -> None:
        self.calls.append(("enable_pane_titles",))

    def set_extended_keys_off(self) -> None:
        self.calls.append(("set_extended_keys_off",))

    def window_exists(self, name: str) -> bool:
        return name in self.windows

    def new_window(self, name: str, command: str) -> None:
        self.calls.append(("new_window", name, command))
        self.windows.add(name)

    def attach(self) -> None:
        self.calls.append(("attach",))

    def set_pane_title(self, pane_id: str, title: str) -> None:
        self.calls.append(("set_pane_title", pane_id, title))

    def focused_pane_id(self):
        return self._focused

    def run(self, args, *, stdin=None) -> str:
        self.calls.append(("run", tuple(args)))
        return ""


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    ft = FakeTmux(server=True, windows=set(), focused="%1")
    monkeypatch.setattr(cli, "tmuxio", ft)
    pidfile = tmp_path / "daemon.pid"
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    return ft, pidfile


def test_up_creates_voice_window_when_absent(fake_env):
    ft, pidfile = fake_env
    rc = cli.main(["up"])
    assert rc == 0
    names = [c for c in ft.calls if c[0] == "new_window"]
    assert names and names[0][1] == "voice"
    assert names[0][2] == "python -m vtmux _daemon"
    assert ("enable_pane_titles",) in ft.calls


def test_up_skips_window_when_present(fake_env):
    ft, pidfile = fake_env
    ft.windows.add("voice")
    rc = cli.main(["up"])
    assert rc == 0
    assert not any(c[0] == "new_window" for c in ft.calls)


def test_up_starts_server_when_down(monkeypatch, tmp_path):
    ft = FakeTmux(server=False, windows=set())
    monkeypatch.setattr(cli, "tmuxio", ft)
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    rc = cli.main(["up"])
    assert rc == 0
    # new-session issued via tmuxio.run WITHOUT a redundant leading "tmux"
    run_calls = [c for c in ft.calls if c[0] == "run"]
    assert ["new-session", "-d", "-s", "vtmux"] in [list(c[1]) for c in run_calls]
    assert all(c[1][0] != "tmux" for c in run_calls)  # run() prepends tmux itself


def test_default_no_subcommand_attaches(fake_env):
    ft, pidfile = fake_env
    rc = cli.main([])
    assert rc == 0
    assert ("attach",) in ft.calls


# ---------------------------------------------------------------------------
# name subcommand
# ---------------------------------------------------------------------------

def _stub_registry(monkeypatch, panes):
    """Force cli to build a PaneRegistry whose .panes returns the given Pane list."""

    class FakeRegistry:
        def __init__(self, *a, **k):
            pass

        def refresh(self):
            pass

        @property
        def panes(self):
            return panes

    monkeypatch.setattr(cli, "PaneRegistry", FakeRegistry)


def _pane(name, pane_id="%2"):
    from vtmux.registry import Pane
    return Pane(id=pane_id, window_id="@1", window="w", index=1,
                name=name, command="claude", active=True)


def test_name_sets_pane_title_on_focused(fake_env, monkeypatch):
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("alpha")])
    rc = cli.main(["name", "beta"])
    assert rc == 0
    titled = [c for c in ft.calls if c[0] == "set_pane_title"]
    assert titled == [("set_pane_title", "%1", "beta")]


def test_name_explicit_pane_arg(fake_env, monkeypatch):
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("alpha")])
    rc = cli.main(["name", "beta", "%7"])
    assert rc == 0
    assert ("set_pane_title", "%7", "beta") in ft.calls


def test_name_rejects_colliding_name(fake_env, monkeypatch, capsys):
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("alpha")])
    rc = cli.main(["name", "alpha"])
    assert rc != 0
    assert not any(c[0] == "set_pane_title" for c in ft.calls)
    captured = capsys.readouterr(); out = captured.out + captured.err
    assert "alpha" in out


# ---------------------------------------------------------------------------
# doctor and down subcommands
# ---------------------------------------------------------------------------

def test_doctor_prints_hints(fake_env, monkeypatch, capsys):
    from vtmux.permissions import PermissionStatus
    status = PermissionStatus(microphone=False, input_monitoring=True, accessibility=True)
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    monkeypatch.setattr(cli, "hints", lambda s: ["grant Microphone in System Settings"])
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "grant Microphone in System Settings" in out


def test_down_terminates_and_removes_pidfile(monkeypatch, tmp_path):
    ft = FakeTmux(server=True)
    monkeypatch.setattr(cli, "tmuxio", ft)
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("4242")
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    rc = cli.main(["down"])
    assert rc == 0
    assert killed == [(4242, cli.signal.SIGTERM)]
    assert not pidfile.exists()


def test_down_no_pidfile_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "tmuxio", FakeTmux())
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "missing.pid")
    rc = cli.main(["down"])
    assert rc == 0


# ---------------------------------------------------------------------------
# status and _daemon subcommands
# ---------------------------------------------------------------------------

def test_status_prints_panes_and_pidfile_and_permissions(fake_env, monkeypatch, capsys):
    from vtmux.permissions import PermissionStatus
    ft, pidfile = fake_env
    pidfile.write_text("999")
    _stub_registry(monkeypatch, [_pane("alpha", "%1"), _pane("beta", "%2")])
    monkeypatch.setattr(
        cli, "check_permissions",
        lambda **k: PermissionStatus(microphone=True, input_monitoring=True, accessibility=True),
    )
    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out
    assert "%1" in out
    assert "999" in out          # pidfile contents shown
    assert "microphone" in out.lower()


def test_daemon_builds_and_runs(monkeypatch, tmp_path):
    import os as _os
    monkeypatch.setattr(cli, "tmuxio", FakeTmux())
    pidfile = tmp_path / "daemon.pid"
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    built = {}

    class FakeDaemon:
        def __init__(self, config, recorder, transcriber, registry, feedback,
                     *, route_fn=None, inject_fn=None):
            built["config"] = config
            built["transcriber"] = transcriber
            built["ran"] = False

        def run(self):
            built["ran"] = True

    monkeypatch.setattr(cli, "Daemon", FakeDaemon)
    monkeypatch.setattr(cli, "Recorder", lambda *a, **k: object())
    monkeypatch.setattr(cli, "ParakeetTranscriber", lambda model_id: ("T", model_id))
    monkeypatch.setattr(cli, "PaneRegistry", lambda *a, **k: object())
    monkeypatch.setattr(cli, "Feedback", lambda *a, **k: object())

    rc = cli.main(["_daemon"])
    assert rc == 0
    assert built["ran"] is True
    assert built["transcriber"][0] == "T"
    assert pidfile.exists()
    assert pidfile.read_text().strip() == str(_os.getpid())


# ---------------------------------------------------------------------------
# Parser coverage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    [], ["up"], ["down"], ["status"], ["doctor"],
    ["name", "x"], ["name", "x", "%3"], ["_daemon"],
])
def test_parser_accepts_all_subcommands(argv):
    parser = cli.build_parser()
    ns = parser.parse_args(argv)
    assert callable(ns.func)


def test_daemon_subcommand_is_hidden_in_help():
    parser = cli.build_parser()
    help_text = parser.format_help()
    assert "_daemon" not in help_text   # hidden, though still parseable (above)
