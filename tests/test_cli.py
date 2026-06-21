import sys

import pytest

from voxpane import cli


class FakeTmux:
    """In-memory stand-in for the tmuxio module."""

    def __init__(self, *, server=True, focused="%1", inside_tmux=False):
        self._server = server
        self._focused = focused
        self._inside_tmux = inside_tmux
        self.calls: list[tuple] = []
        self.daemon_spawns: list = []

    def server_running(self) -> bool:
        return self._server

    def enable_pane_titles(self) -> None:
        self.calls.append(("enable_pane_titles",))

    def set_extended_keys_off(self) -> None:
        self.calls.append(("set_extended_keys_off",))

    def install_status_indicator(self) -> None:
        self.calls.append(("install_status_indicator",))

    def restore_status_right(self) -> None:
        self.calls.append(("restore_status_right",))

    def inside_tmux(self) -> bool:
        return self._inside_tmux

    def attach(self) -> None:
        self.calls.append(("attach",))

    def set_pane_name(self, pane_id: str, name: str) -> None:
        self.calls.append(("set_pane_name", pane_id, name))

    def set_pane_autoname_hooks(self, self_cmd: str) -> None:
        self.calls.append(("set_pane_autoname_hooks", self_cmd))

    def bind_rename_key(self, self_cmd: str, key: str = "R") -> None:
        self.calls.append(("bind_rename_key", self_cmd, key))

    def focused_pane_id(self):
        return self._focused

    def run(self, args, *, stdin=None) -> str:
        self.calls.append(("run", tuple(args)))
        return ""


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    ft = FakeTmux(server=True, focused="%1")
    monkeypatch.setattr(cli, "tmuxio", ft)
    pidfile = tmp_path / "daemon.pid"
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    # Pretend a config already exists so `setup`'s first-run journal prompt is a
    # no-op (it must never block on stdin in the unit suite).
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setattr(cli, "CONFIG_PATH", cfg)
    # `setup` lists mic devices; keep it from shelling out to system_profiler /
    # blocking on stdin. Tests that exercise the mic flow override this.
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: [])
    # Don't launch a real background process or probe a real pid in unit tests.
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)
    monkeypatch.setattr(cli, "_spawn_daemon", lambda: ft.daemon_spawns.append(True))
    # ensure_up sweeps unnamed panes via PaneRegistry; default to an empty
    # registry so unit tests never touch a real tmux. Tests override as needed.
    _stub_registry(monkeypatch, [])
    return ft, pidfile


def test_up_spawns_daemon_when_not_running(fake_env):
    ft, pidfile = fake_env
    rc = cli.main(["up"])
    assert rc == 0
    # The daemon runs as a detached background process, NOT in a tmux window.
    assert ft.daemon_spawns == [True]
    assert ("enable_pane_titles",) in ft.calls


def test_up_installs_status_indicator_by_default(fake_env):
    ft, _ = fake_env
    assert cli.main(["up"]) == 0
    assert ("install_status_indicator",) in ft.calls


def test_up_skips_status_indicator_when_disabled(fake_env, monkeypatch):
    from voxpane.config import Config
    ft, _ = fake_env
    monkeypatch.setattr(cli, "load_config", lambda: Config(status_indicator=False))
    assert cli.main(["up"]) == 0
    assert ("install_status_indicator",) not in ft.calls
    assert ("restore_status_right",) in ft.calls  # opted out: status-right handed back


def test_up_installs_naming_hooks_and_binding(fake_env):
    ft, pidfile = fake_env
    rc = cli.main(["up"])
    assert rc == 0
    kinds = [c[0] for c in ft.calls]
    assert "set_pane_autoname_hooks" in kinds  # new panes auto-named
    assert "bind_rename_key" in kinds          # prefix+R renames active pane


def test_up_names_unnamed_initial_pane(fake_env, monkeypatch):
    # The session's first pane is created by new-session (no split hook fires),
    # so ensure_up's sweep must give it a callsign.
    from voxpane.router import CALLSIGNS
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("%0", "%0")])  # one unnamed pane
    rc = cli.main(["up"])
    assert rc == 0
    assert ("set_pane_name", "%0", CALLSIGNS[0]) in ft.calls


def test_autoname_unnamed_panes_sweeps_only_unnamed(fake_env, monkeypatch):
    from voxpane.router import CALLSIGNS
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [
        _pane(CALLSIGNS[0], "%1"),  # already named -> skipped, but its name is "used"
        _pane("%2", "%2"),          # unnamed
        _pane("%3", "%3"),          # unnamed
    ])
    cli._autoname_unnamed_panes()
    named = [c for c in ft.calls if c[0] == "set_pane_name"]
    # Each unnamed pane gets a distinct callsign, skipping the one already in use.
    assert named == [
        ("set_pane_name", "%2", CALLSIGNS[1]),
        ("set_pane_name", "%3", CALLSIGNS[2]),
    ]


def test_up_skips_spawn_when_daemon_already_running(fake_env, monkeypatch):
    ft, pidfile = fake_env
    monkeypatch.setattr(cli, "_daemon_running", lambda: True)
    rc = cli.main(["up"])
    assert rc == 0
    assert ft.daemon_spawns == []


def test_up_starts_server_when_down(monkeypatch, tmp_path):
    ft = FakeTmux(server=False)
    monkeypatch.setattr(cli, "tmuxio", ft)
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)
    monkeypatch.setattr(cli, "_spawn_daemon", lambda: None)
    # Force the agent-missing path so the asserted argv is the plain shell form.
    monkeypatch.setattr(cli.shutil, "which", lambda c: None)
    _stub_registry(monkeypatch, [])  # ensure_up sweeps the registry; keep it hermetic
    rc = cli.main(["up"])
    assert rc == 0
    # new-session issued via tmuxio.run WITHOUT a redundant leading "tmux"
    run_calls = [c for c in ft.calls if c[0] == "run"]
    assert ["new-session", "-d", "-s", "voxpane"] in [list(c[1]) for c in run_calls]
    assert all(c[1][0] != "tmux" for c in run_calls)  # run() prepends tmux itself


def test_up_opens_initial_pane_on_agent_when_installed(monkeypatch, tmp_path):
    # Agent-first: when `pane_command` is on PATH, the session's first pane runs it.
    ft = FakeTmux(server=False)
    monkeypatch.setattr(cli, "tmuxio", ft)
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)
    monkeypatch.setattr(cli, "_spawn_daemon", lambda: None)
    monkeypatch.setattr(cli.shutil, "which", lambda c: "/usr/bin/claude")
    _stub_registry(monkeypatch, [])
    rc = cli.main(["up"])
    assert rc == 0
    run_calls = [list(c[1]) for c in ft.calls if c[0] == "run"]
    # The agent is wrapped so the pane drops to a shell when it exits.
    assert ["new-session", "-d", "-s", "voxpane",
            "claude; exec ${SHELL:-/bin/sh} -i"] in run_calls


def test_up_falls_back_to_shell_when_agent_missing(monkeypatch, tmp_path, capsys):
    # Missing agent must degrade to a plain shell (a command that exits at once
    # would kill the session) and warn the user.
    ft = FakeTmux(server=False)
    monkeypatch.setattr(cli, "tmuxio", ft)
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)
    monkeypatch.setattr(cli, "_spawn_daemon", lambda: None)
    monkeypatch.setattr(cli.shutil, "which", lambda c: None)
    _stub_registry(monkeypatch, [])
    rc = cli.main(["up"])
    assert rc == 0
    run_calls = [list(c[1]) for c in ft.calls if c[0] == "run"]
    assert ["new-session", "-d", "-s", "voxpane"] in run_calls  # no trailing program
    assert "not found on PATH" in capsys.readouterr().out


def test_spawn_daemon_uses_venv_interpreter_and_detaches(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "DAEMON_LOG", tmp_path / "daemon.log")
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.pid = 4321

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    cli._spawn_daemon()
    # Must use the venv interpreter so the daemon imports voxpane's deps.
    assert captured["argv"][0] == sys.executable
    assert captured["argv"][1:] == ["-m", "voxpane", "_daemon"]
    # Must detach from the controlling terminal.
    assert captured["kwargs"].get("start_new_session") is True
    # pid recorded for `down`/`status`.
    assert (tmp_path / "daemon.pid").read_text().strip() == "4321"


def test_default_no_subcommand_attaches(fake_env):
    ft, pidfile = fake_env
    rc = cli.main([])
    assert rc == 0
    assert ("attach",) in ft.calls


def test_default_reload_respawns_daemon_then_attaches(fake_env, monkeypatch):
    # `voxpane --reload` collapses `reload && voxpane`: kill the running daemon,
    # respawn it (so source edits load), then attach.
    ft, pidfile = fake_env
    pidfile.write_text("4321")
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    rc = cli.main(["--reload"])

    assert rc == 0
    assert killed == [(4321, cli.signal.SIGTERM)]
    assert not pidfile.exists()  # _cmd_down clears the stale pidfile
    assert ft.daemon_spawns == [True]  # ensure_up respawned the daemon
    assert ("attach",) in ft.calls


def test_default_reload_inside_tmux_skips_attach(fake_env, monkeypatch, capsys):
    # `tmux attach` refuses to nest; from inside a pane the reload must respawn
    # the daemon but skip the attach instead of failing.
    ft, pidfile = fake_env
    ft._inside_tmux = True
    pidfile.write_text("4321")
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)

    rc = cli.main(["--reload"])

    assert rc == 0
    assert ft.daemon_spawns == [True]  # daemon still respawned
    assert ("attach",) not in ft.calls  # but no nesting attach
    assert "nesting" in capsys.readouterr().out


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
    from voxpane.registry import Pane
    return Pane(id=pane_id, window_id="@1", window="w", index=1,
                name=name, command="claude", active=True)


def test_name_sets_pane_name_on_focused(fake_env, monkeypatch):
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("alpha")])
    rc = cli.main(["name", "beta"])
    assert rc == 0
    named = [c for c in ft.calls if c[0] == "set_pane_name"]
    assert named == [("set_pane_name", "%1", "beta")]


def test_name_explicit_pane_arg(fake_env, monkeypatch):
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("alpha")])
    rc = cli.main(["name", "beta", "%7"])
    assert rc == 0
    assert ("set_pane_name", "%7", "beta") in ft.calls


def test_name_rejects_broadcast_word(fake_env, monkeypatch, capsys):
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [])
    rc = cli.main(["name", "everyone"])
    assert rc == 1
    assert not any(c[0] == "set_pane_name" for c in ft.calls)
    assert "reserved" in capsys.readouterr().out


def test_name_rejects_colliding_name(fake_env, monkeypatch, capsys):
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("alpha")])
    rc = cli.main(["name", "alpha"])
    assert rc != 0
    assert not any(c[0] == "set_pane_name" for c in ft.calls)
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "alpha" in out


# ---------------------------------------------------------------------------
# autoname subcommand (called by the tmux pane-creation hooks)
# ---------------------------------------------------------------------------

def test_autoname_assigns_callsign_to_unnamed_focused(fake_env, monkeypatch):
    from voxpane.router import CALLSIGNS
    ft, pidfile = fake_env                       # focused pane is %1
    _stub_registry(monkeypatch, [_pane("%1", "%1")])  # unnamed: name == id
    rc = cli.main(["autoname"])
    assert rc == 0
    assert ("set_pane_name", "%1", CALLSIGNS[0]) in ft.calls


def test_autoname_skips_already_named_pane(fake_env, monkeypatch, capsys):
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("alpha", "%1")])  # already named
    rc = cli.main(["autoname"])
    assert rc == 0
    assert not any(c[0] == "set_pane_name" for c in ft.calls)
    assert "alpha" in capsys.readouterr().out


def test_autoname_explicit_pane_arg(fake_env, monkeypatch):
    from voxpane.router import CALLSIGNS
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("%7", "%7")])
    rc = cli.main(["autoname", "%7"])
    assert rc == 0
    assert ("set_pane_name", "%7", CALLSIGNS[0]) in ft.calls


def test_autoname_avoids_callsign_already_in_use(fake_env, monkeypatch):
    from voxpane.router import CALLSIGNS
    ft, pidfile = fake_env
    # CALLSIGNS[0] is taken by another pane; the unnamed one must get CALLSIGNS[1].
    _stub_registry(monkeypatch, [_pane(CALLSIGNS[0], "%2"), _pane("%1", "%1")])
    rc = cli.main(["autoname"])
    assert rc == 0
    assert ("set_pane_name", "%1", CALLSIGNS[1]) in ft.calls


# ---------------------------------------------------------------------------
# doctor and down subcommands
# ---------------------------------------------------------------------------

def test_doctor_prints_hints(fake_env, monkeypatch, capsys):
    from voxpane.permissions import PermissionStatus
    status = PermissionStatus(microphone=False, input_monitoring=True, accessibility=True)
    monkeypatch.setattr(cli, "missing_tools", lambda: [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    monkeypatch.setattr(cli, "hints", lambda s: ["grant Microphone in System Settings"])
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "grant Microphone in System Settings" in out


def test_doctor_reports_missing_sox_and_skips_mic_hint(fake_env, monkeypatch, capsys):
    # When sox is absent, the real fix is "install sox" - NOT "grant Microphone".
    from voxpane.permissions import PermissionStatus
    status = PermissionStatus(microphone=False, input_monitoring=True, accessibility=True)
    monkeypatch.setattr(cli, "missing_tools", lambda: ["sox"])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    # use the real hints() (its mic line starts with "Microphone:")
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "brew install sox" in out
    assert "Microphone" not in out  # misleading mic hint suppressed when sox missing


def test_setup_all_granted_reports_ready(fake_env, monkeypatch, capsys):
    from voxpane.permissions import PermissionStatus, TerminalApp
    status = PermissionStatus(microphone=True, input_monitoring=True, accessibility=True)
    monkeypatch.setattr(cli, "missing_tools", lambda: [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    monkeypatch.setattr(cli, "model_cached", lambda mid: True)  # no real download
    monkeypatch.setattr(cli, "terminal_app", lambda: TerminalApp("Terminal", "com.apple.Terminal"))
    opened: list[str] = []
    monkeypatch.setattr(cli, "open_settings_pane", lambda url: opened.append(url))
    rc = cli.main(["setup"])
    assert rc == 0
    assert opened == []  # nothing to open
    assert "ready" in capsys.readouterr().out.lower()


def test_setup_opens_panes_for_missing_permissions(fake_env, monkeypatch, capsys):
    from voxpane.permissions import PermissionStatus, TerminalApp
    status = PermissionStatus(microphone=False, input_monitoring=True, accessibility=False)
    monkeypatch.setattr(cli, "missing_tools", lambda: [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    monkeypatch.setattr(cli, "model_cached", lambda mid: True)  # no real download
    ghostty = TerminalApp("Ghostty", "com.mitchellh.ghostty")
    monkeypatch.setattr(cli, "terminal_app", lambda: ghostty)
    opened: list[str] = []
    monkeypatch.setattr(cli, "open_settings_pane", lambda url: opened.append(url))
    rc = cli.main(["setup"])
    assert rc == 1  # not fully granted yet
    out = capsys.readouterr().out
    assert "Ghostty" in out
    assert "tccutil reset Microphone com.mitchellh.ghostty" in out
    # one deep link opened per failing permission (mic + accessibility)
    assert any("Privacy_Microphone" in u for u in opened)
    assert any("Privacy_Accessibility" in u for u in opened)
    assert len(opened) == 2


def test_setup_aborts_when_tools_missing(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "missing_tools", lambda: ["sox"])
    rc = cli.main(["setup"])
    assert rc == 1
    assert "brew install sox" in capsys.readouterr().out


def test_setup_downloads_model_when_missing(fake_env, monkeypatch, capsys):
    from voxpane.permissions import PermissionStatus, TerminalApp
    status = PermissionStatus(microphone=True, input_monitoring=True, accessibility=True)
    monkeypatch.setattr(cli, "missing_tools", lambda: [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    monkeypatch.setattr(cli, "model_cached", lambda mid: False)
    monkeypatch.setattr(cli, "terminal_app", lambda: TerminalApp("Terminal", "com.apple.Terminal"))
    monkeypatch.setattr(cli, "open_settings_pane", lambda url: None)
    warmed: list[str] = []

    class FakeTranscriber:
        def __init__(self, model_id):
            self.model_id = model_id

        def warm(self):
            warmed.append(self.model_id)

    monkeypatch.setattr(cli, "ParakeetTranscriber", FakeTranscriber)
    rc = cli.main(["setup"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "downloading" in out.lower() or "fetching" in out.lower()
    assert warmed  # the model was pre-downloaded in the foreground


def test_setup_skips_download_when_model_cached(fake_env, monkeypatch, capsys):
    from voxpane.permissions import PermissionStatus, TerminalApp
    status = PermissionStatus(microphone=True, input_monitoring=True, accessibility=True)
    monkeypatch.setattr(cli, "missing_tools", lambda: [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    monkeypatch.setattr(cli, "model_cached", lambda mid: True)
    monkeypatch.setattr(cli, "terminal_app", lambda: TerminalApp("Terminal", "com.apple.Terminal"))
    monkeypatch.setattr(cli, "open_settings_pane", lambda url: None)

    def _boom(model_id):
        raise AssertionError("must not construct/download when cached")

    monkeypatch.setattr(cli, "ParakeetTranscriber", _boom)
    rc = cli.main(["setup"])
    assert rc == 0
    assert "speech model: ready" in capsys.readouterr().out.lower()


def test_doctor_warns_when_model_not_downloaded(fake_env, monkeypatch, capsys):
    from voxpane.permissions import PermissionStatus
    status = PermissionStatus(microphone=True, input_monitoring=True, accessibility=True)
    monkeypatch.setattr(cli, "missing_tools", lambda: [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    monkeypatch.setattr(cli, "model_cached", lambda mid: False)
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "speech model not downloaded" in out.lower()
    assert "All checks passed." not in out  # the model gap can't be hidden


def test_doctor_all_passed_includes_model(fake_env, monkeypatch, capsys):
    from voxpane.permissions import PermissionStatus
    status = PermissionStatus(microphone=True, input_monitoring=True, accessibility=True)
    monkeypatch.setattr(cli, "missing_tools", lambda: [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    monkeypatch.setattr(cli, "model_cached", lambda mid: True)
    rc = cli.main(["doctor"])
    assert rc == 0
    assert "All checks passed." in capsys.readouterr().out


def test_status_reports_model_state(fake_env, monkeypatch, capsys):
    from voxpane.permissions import PermissionStatus
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)
    _stub_registry(monkeypatch, [])
    monkeypatch.setattr(
        cli, "check_permissions",
        lambda **k: PermissionStatus(microphone=True, input_monitoring=True, accessibility=True),
    )
    monkeypatch.setattr(cli, "model_cached", lambda mid: False)
    rc = cli.main(["status"])
    assert rc == 0
    assert "not downloaded" in capsys.readouterr().out.lower()


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


def test_down_without_pidfile_is_noop(monkeypatch, tmp_path):
    # No pidfile (daemon crashed before writing it, or already stopped): down must
    # not os.kill anything and must not crash. The daemon owns no tmux window.
    ft = FakeTmux(server=True)
    monkeypatch.setattr(cli, "tmuxio", ft)
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "missing.pid")
    killed: list = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    rc = cli.main(["down"])
    assert rc == 0
    assert killed == []                        # no pid -> no os.kill


def test_reload_stops_then_restarts_daemon(monkeypatch, tmp_path):
    # reload = down (SIGTERM + clear pidfile) then ensure_up (respawn).
    ft = FakeTmux(server=True)
    monkeypatch.setattr(cli, "tmuxio", ft)
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("4242")
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    _stub_registry(monkeypatch, [])  # ensure_up sweeps the registry; keep it hermetic
    spawns: list[bool] = []
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)
    monkeypatch.setattr(cli, "_spawn_daemon", lambda: spawns.append(True))
    rc = cli.main(["reload"])
    assert rc == 0
    assert killed == [(4242, cli.signal.SIGTERM)]  # old daemon terminated
    assert spawns == [True]                         # fresh daemon spawned


# ---------------------------------------------------------------------------
# status and _daemon subcommands
# ---------------------------------------------------------------------------

def test_status_prints_panes_and_pidfile_and_permissions(fake_env, monkeypatch, capsys):
    from voxpane.permissions import PermissionStatus
    ft, pidfile = fake_env
    pidfile.write_text("999")
    monkeypatch.setattr(cli, "_daemon_running", lambda: True)
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
# voice-commands subcommand
# ---------------------------------------------------------------------------

def test_voice_commands_text_lists_verbs_and_words():
    from voxpane.commands import _CLOSE_VERBS, _CREATE_VERBS
    from voxpane.config import Config
    text = cli._voice_commands_text(Config())  # button is the default mode
    # Every create/close verb the parser accepts must appear in the cheat sheet.
    for verb in (*_CREATE_VERBS, *_CLOSE_VERBS):
        assert verb in text
    assert "focus" in text and "swap" in text
    # Config-driven broadcast word.
    assert "everyone" in text


def test_voice_commands_text_keyword_mode_has_no_command_layer():
    from voxpane.config import Config
    text = cli._voice_commands_text(Config(addressing="keyword"))
    assert "keyword" in text
    assert "no command layer" in text       # commands live on the button system key
    assert "create <n> panes" not in text   # no command table in keyword mode


def test_voice_commands_text_button_mode_shows_both_keys():
    from voxpane.config import Config
    cfg = Config(addressing="button", hotkey="alt_r", command_hotkey="alt_l")
    text = cli._voice_commands_text(cfg)
    assert "button" in text
    assert "alt_l" in text and "alt_r" in text
    assert "computer create" not in text  # no control-word prefix in button mode


def test_voice_commands_text_lists_configured_macros():
    from voxpane.config import Config
    cfg = Config(macros={"set up": ["create two panes", "tile"]})
    text = cli._voice_commands_text(cfg)
    assert "set up" in text
    assert "create two panes" in text


def test_voice_commands_prints(fake_env, monkeypatch, capsys):
    from voxpane.config import Config
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: Config())
    rc = cli.main(["voice-commands"])
    assert rc == 0
    assert "voice commands" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Parser coverage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    [], ["up"], ["down"], ["reload"], ["status"], ["doctor"], ["voice-commands"],
    ["name", "x"], ["name", "x", "%3"], ["autoname"], ["autoname", "%3"],
    ["_daemon"],
])
def test_parser_accepts_all_subcommands(argv):
    parser = cli.build_parser()
    ns = parser.parse_args(argv)
    assert callable(ns.func)


def test_daemon_subcommand_is_hidden_in_help():
    parser = cli.build_parser()
    help_text = parser.format_help()
    assert "_daemon" not in help_text   # hidden, though still parseable (above)


def test_prompt_yes_no_default_on_empty():
    assert cli._prompt_yes_no("q?", default=True, reader=lambda _: "") is True
    assert cli._prompt_yes_no("q?", default=False, reader=lambda _: "") is False


def test_prompt_yes_no_parses_answer():
    assert cli._prompt_yes_no("q?", default=False, reader=lambda _: "y") is True
    assert cli._prompt_yes_no("q?", default=True, reader=lambda _: "n") is False


def test_prompt_yes_no_eof_keeps_default():
    def boom(_):
        raise EOFError
    assert cli._prompt_yes_no("q?", default=True, reader=boom) is True


def test_prompt_journal_setup_writes_choices(tmp_path):
    p = tmp_path / "config.toml"
    answers = iter(["y", "y"])  # journal? yes; audio? yes
    cli._prompt_journal_setup(reader=lambda _: next(answers), config_path=p)
    cfg = cli.load_config(p)
    assert cfg.journal_enabled is True
    assert cfg.journal_keep_audio is True


def test_prompt_journal_setup_skips_audio_when_disabled(tmp_path):
    p = tmp_path / "config.toml"
    # Only one prompt should be consumed when journaling is declined.
    answers = iter(["n"])
    cli._prompt_journal_setup(reader=lambda _: next(answers), config_path=p)
    cfg = cli.load_config(p)
    assert cfg.journal_enabled is False
    assert cfg.journal_keep_audio is False


def test_prompt_journal_setup_noop_when_config_exists(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('hotkey = "ctrl_r"\n')

    def boom(_):
        raise AssertionError("should not prompt when config exists")
    cli._prompt_journal_setup(reader=boom, config_path=p)
    assert p.read_text() == 'hotkey = "ctrl_r"\n'  # untouched


def test_setup_prompts_journal_on_first_run(fake_env, monkeypatch, tmp_path, capsys):
    from voxpane.permissions import PermissionStatus, TerminalApp
    # Point CONFIG_PATH at a missing file so the first-run prompt fires.
    cfg = tmp_path / "fresh" / "config.toml"
    monkeypatch.setattr(cli, "CONFIG_PATH", cfg)
    status = PermissionStatus(microphone=True, input_monitoring=True, accessibility=True)
    monkeypatch.setattr(cli, "missing_tools", lambda: [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    monkeypatch.setattr(cli, "model_cached", lambda mid: True)
    monkeypatch.setattr(cli, "terminal_app", lambda: TerminalApp("Terminal", "com.apple.Terminal"))
    monkeypatch.setattr(cli, "open_settings_pane", lambda url: None)
    answers = iter(["y", "n"])  # journal yes, audio no
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    rc = cli.main(["setup"])
    assert rc == 0
    written = cli.load_config(cfg)
    assert written.journal_enabled is True
    assert written.journal_keep_audio is False


# ---------------------------------------------------------------------------
# mic command + setup mic step
# ---------------------------------------------------------------------------

def _devs():
    from voxpane.audio import InputDevice
    return [
        InputDevice("Built-in Microphone", is_default=True),
        InputDevice("AirPods Pro", is_default=False),
    ]


def test_mic_lists_devices_and_marks_default(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    monkeypatch.setattr(cli, "load_config", lambda: cli.Config(mic_device=""))
    rc = cli.main(["mic"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Built-in Microphone" in out
    assert "default" in out
    assert "system default" in out  # hint when nothing pinned


def test_mic_marks_current_selection(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    monkeypatch.setattr(
        cli, "load_config", lambda: cli.Config(mic_device="AirPods Pro"))
    rc = cli.main(["mic"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "selected" in out
    assert "Pinned: AirPods Pro" in out


def test_mic_select_by_index_persists(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    saved = {}
    monkeypatch.setattr(
        cli, "set_mic_device", lambda name: saved.setdefault("name", name))
    rc = cli.main(["mic", "1"])
    assert rc == 0
    assert saved["name"] == "AirPods Pro"
    assert "Mic set to: AirPods Pro" in capsys.readouterr().out


def test_mic_select_by_name_persists(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    saved = {}
    monkeypatch.setattr(
        cli, "set_mic_device", lambda name: saved.setdefault("name", name))
    rc = cli.main(["mic", "airpods pro"])  # case-insensitive
    assert rc == 0
    assert saved["name"] == "AirPods Pro"


def test_mic_default_clears_pin(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    saved = {}
    monkeypatch.setattr(
        cli, "set_mic_device", lambda name: saved.setdefault("name", name))
    rc = cli.main(["mic", "default"])
    assert rc == 0
    assert saved["name"] == ""
    assert "system default" in capsys.readouterr().out


def test_mic_bad_index_errors_without_writing(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    wrote = []
    monkeypatch.setattr(cli, "set_mic_device", lambda name: wrote.append(name))
    rc = cli.main(["mic", "9"])
    assert rc == 1
    assert wrote == []
    assert "No device at index 9" in capsys.readouterr().out


def test_mic_no_devices_reports_and_fails(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: [])
    rc = cli.main(["mic"])
    assert rc == 1
    assert "No input devices" in capsys.readouterr().out


def test_setup_mic_prompt_persists_choice(fake_env, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    cfgpath = tmp_path / "config.toml"
    cfgpath.write_text("")
    saved = {}
    monkeypatch.setattr(
        cli, "set_mic_device",
        lambda name, path=None: saved.setdefault("name", name))
    monkeypatch.setattr("builtins.input", lambda _: "1")
    cli._prompt_mic_setup(config_path=cfgpath)
    assert saved["name"] == "AirPods Pro"


def test_setup_mic_prompt_bare_enter_keeps_current(fake_env, monkeypatch):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    wrote = []
    monkeypatch.setattr(
        cli, "set_mic_device", lambda name, path=None: wrote.append(name))
    monkeypatch.setattr("builtins.input", lambda _: "")
    cli._prompt_mic_setup()
    assert wrote == []  # no change on empty input
