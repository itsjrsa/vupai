import os
import sys

import pytest

from vupai import cli


class FakeTmux:
    """In-memory stand-in for the tmuxio module."""

    def __init__(self, *, server=True, focused="%1", inside_tmux=False,
                 inside_vupai=True, footprint=False):
        self._server = server
        self._focused = focused
        self._inside_tmux = inside_tmux
        # When inside tmux, is it vupai's own server (switch-client) or the
        # user's other tmux (cross-socket attach)? Defaults to vupai's.
        self._inside_vupai = inside_vupai
        self._footprint = footprint
        self._board_pane = None        # set to a pane id to simulate an open board
        self.calls: list[tuple] = []
        self.daemon_spawns: list = []

    def server_running(self) -> bool:
        return self._server

    def has_session(self, name: str) -> bool:
        # The suite models a single pre-existing session, so "server up" doubles
        # as "the target session already exists".
        return self._server

    def enable_pane_titles(self) -> None:
        self.calls.append(("enable_pane_titles",))

    def set_terminal_title(self) -> None:
        self.calls.append(("set_terminal_title",))

    def set_extended_keys_off(self) -> None:
        self.calls.append(("set_extended_keys_off",))

    def set_base_index(self) -> None:
        self.calls.append(("set_base_index",))

    def install_status_indicator(self) -> None:
        self.calls.append(("install_status_indicator",))

    def restore_status_right(self) -> None:
        self.calls.append(("restore_status_right",))

    def install_tip_segment(self) -> None:
        self.calls.append(("install_tip_segment",))

    def restore_status_left(self) -> None:
        self.calls.append(("restore_status_left",))

    def inside_tmux(self) -> bool:
        return self._inside_tmux

    def socket_env_prefix(self) -> str:
        return ""

    def inside_vupai_server(self) -> bool:
        return self._inside_tmux and self._inside_vupai

    def default_server_footprint(self) -> bool:
        return self._footprint

    def cleanup_default_server(self) -> None:
        self.calls.append(("cleanup_default_server",))

    def attach(self, target: str | None = None) -> None:
        self.calls.append(("attach", target))

    def switch_client(self, name: str) -> None:
        self.calls.append(("switch_client", name))

    def kill_session(self, name: str) -> None:
        self.calls.append(("kill_session", name))

    def set_pane_name(self, pane_id: str, name: str) -> None:
        self.calls.append(("set_pane_name", pane_id, name))

    def set_pane_program(self, pane_id: str, label: str) -> None:
        self.calls.append(("set_pane_program", pane_id, label))

    def set_pane_autoname_hooks(self, self_cmd: str) -> None:
        self.calls.append(("set_pane_autoname_hooks", self_cmd))

    def bind_rename_key(self, self_cmd: str, key: str = "R") -> None:
        self.calls.append(("bind_rename_key", self_cmd, key))

    def focused_pane_id(self):
        return self._focused

    def split_window(self, target, program, *, horizontal=False, size=None):
        self.calls.append(("split_window", target, program, horizontal, size))
        return "%7"

    def mark_board_pane(self, pane_id):
        self.calls.append(("mark_board_pane", pane_id))

    def pane_session(self, pane_id):
        return "repo"

    def find_board_pane(self, session):
        return self._board_pane

    def select_pane(self, pane_id):
        self.calls.append(("select_pane", pane_id))

    def run(self, args, *, stdin=None) -> str:
        self.calls.append(("run", tuple(args)))
        # new-session is invoked with -P -F '#{pane_id}', so it prints the new
        # pane id; ensure_up captures it to label the initial pane's program.
        if args and args[0] == "new-session":
            return "%0\n"
        return ""


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    ft = FakeTmux(server=True, focused="%1")
    monkeypatch.setattr(cli, "tmuxio", ft)
    pidfile = tmp_path / "daemon.pid"
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    # Keep the one-shot migration probe off the real home dir.
    monkeypatch.setattr(cli, "MIGRATE_SENTINEL", tmp_path / ".migrated")
    # Pretend a config already exists so `setup`'s first-run journal prompt is a
    # no-op (it must never block on stdin in the unit suite).
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setattr(cli, "CONFIG_PATH", cfg)
    # `setup` lists mic devices; keep it from shelling out to system_profiler /
    # blocking on stdin. Tests that exercise the mic flow override this.
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: [])
    # Mic pinning probes the device with `rec`; default to "usable" so the
    # existing pin tests stay hermetic. Tests that exercise the probe override it.
    monkeypatch.setattr(cli.audio, "probe_capture", lambda *a, **k: None)
    # Don't launch a real background process or probe a real pid in unit tests.
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)
    monkeypatch.setattr(cli, "_spawn_daemon", lambda: ft.daemon_spawns.append(True))
    # `down`/`reload` now verify the recorded pid is really a vupai daemon before
    # signalling it; treat the test pid as ours unless a test overrides this.
    monkeypatch.setattr(cli, "_pid_is_vupai", lambda pid: True)
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
    assert ("set_base_index",) in ft.calls  # 1-based numbering for spoken numbers


def test_up_installs_status_indicator_by_default(fake_env):
    ft, _ = fake_env
    assert cli.main(["up"]) == 0
    assert ("install_status_indicator",) in ft.calls


def test_up_skips_status_indicator_when_disabled(fake_env, monkeypatch):
    from vupai.config import Config
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
    from vupai.router import CALLSIGNS
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("%0", "%0")])  # one unnamed pane
    rc = cli.main(["up"])
    assert rc == 0
    assert ("set_pane_name", "%0", CALLSIGNS[0]) in ft.calls


def test_autoname_unnamed_panes_sweeps_only_unnamed(fake_env, monkeypatch):
    from vupai.router import CALLSIGNS
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
    name = cli._resolve_session_name(None)
    cwd = cli.os.getcwd()
    run_calls = [c for c in ft.calls if c[0] == "run"]
    assert ["new-session", "-d", "-P", "-F", "#{pane_id}",
            "-s", name, "-c", cwd] in [list(c[1]) for c in run_calls]
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
    name = cli._resolve_session_name(None)
    cwd = cli.os.getcwd()
    run_calls = [list(c[1]) for c in ft.calls if c[0] == "run"]
    # The agent is wrapped so the pane drops to a shell when it exits.
    assert ["new-session", "-d", "-P", "-F", "#{pane_id}",
            "-s", name, "-c", cwd,
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
    name = cli._resolve_session_name(None)
    cwd = cli.os.getcwd()
    run_calls = [list(c[1]) for c in ft.calls if c[0] == "run"]
    assert ["new-session", "-d", "-P", "-F", "#{pane_id}",
            "-s", name, "-c", cwd] in run_calls  # no trailing program
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
    # Must use the venv interpreter so the daemon imports vupai's deps.
    assert captured["argv"][0] == sys.executable
    assert captured["argv"][1:] == ["-m", "vupai", "_daemon"]
    # Must detach from the controlling terminal.
    assert captured["kwargs"].get("start_new_session") is True
    # pid recorded for `down`/`status`.
    assert (tmp_path / "daemon.pid").read_text().strip() == "4321"


def test_default_no_subcommand_attaches(fake_env):
    ft, pidfile = fake_env
    rc = cli.main([])
    assert rc == 0
    assert ("attach", cli._resolve_session_name(None)) in ft.calls


def test_attach_named_creates_and_attaches(monkeypatch, tmp_path):
    # `vupai attach backend` creates a session named `backend` (server was down)
    # and attaches to it by exact target.
    ft = FakeTmux(server=False)
    monkeypatch.setattr(cli, "tmuxio", ft)
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)
    monkeypatch.setattr(cli, "_spawn_daemon", lambda: None)
    monkeypatch.setattr(cli.shutil, "which", lambda c: None)
    _stub_registry(monkeypatch, [])
    rc = cli.main(["attach", "backend"])
    assert rc == 0
    run_calls = [list(c[1]) for c in ft.calls if c[0] == "run"]
    cwd = cli.os.getcwd()
    assert ["new-session", "-d", "-P", "-F", "#{pane_id}",
            "-s", "backend", "-c", cwd] in run_calls
    assert ("attach", "backend") in ft.calls


def test_attach_existing_session_skips_create(fake_env):
    # When the target session already exists (server up), no new-session is
    # issued - just an attach to it. The `a` alias works too.
    ft, _ = fake_env
    rc = cli.main(["a", "backend"])
    assert rc == 0
    assert not any(c[0] == "run" and c[1][0] == "new-session" for c in ft.calls)
    assert ("attach", "backend") in ft.calls


def test_attach_inside_vupai_server_switches_client(fake_env):
    # Inside vupai's OWN server, attach would nest, so switch the client instead.
    ft, _ = fake_env
    ft._inside_tmux = True
    ft._inside_vupai = True
    rc = cli.main(["attach", "backend"])
    assert rc == 0
    assert ("switch_client", "backend") in ft.calls
    assert not any(c[0] == "attach" for c in ft.calls)


def test_attach_inside_foreign_tmux_attaches(fake_env):
    # Inside the user's OTHER tmux, switch-client can't cross servers, so vupai
    # does a cross-socket attach into its own server rather than switch.
    ft, _ = fake_env
    ft._inside_tmux = True
    ft._inside_vupai = False
    rc = cli.main(["attach", "backend"])
    assert rc == 0
    assert ("attach", "backend") in ft.calls
    assert not any(c[0] == "switch_client" for c in ft.calls)


def test_new_errors_when_session_exists(fake_env, capsys):
    # `vupai new NAME` refuses a duplicate (like tmux), pointing at `attach`.
    ft, _ = fake_env  # server up => session exists
    rc = cli.main(["new", "backend"])
    assert rc == 1
    assert not any(c[0] == "run" and c[1][0] == "new-session" for c in ft.calls)
    assert "already exists" in capsys.readouterr().out


def test_new_creates_when_absent(monkeypatch, tmp_path):
    ft = FakeTmux(server=False)
    monkeypatch.setattr(cli, "tmuxio", ft)
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)
    monkeypatch.setattr(cli, "_spawn_daemon", lambda: None)
    monkeypatch.setattr(cli.shutil, "which", lambda c: None)
    _stub_registry(monkeypatch, [])
    rc = cli.main(["new", "backend"])
    assert rc == 0
    run_calls = [list(c[1]) for c in ft.calls if c[0] == "run"]
    assert any(c[:7] == ["new-session", "-d", "-P", "-F", "#{pane_id}",
                         "-s", "backend"] for c in run_calls)
    assert ("attach", "backend") in ft.calls


def test_kill_existing_session(fake_env):
    ft, _ = fake_env  # server up => session exists
    rc = cli.main(["kill", "backend"])
    assert rc == 0
    assert ("kill_session", "backend") in ft.calls


def test_kill_missing_session_reports(monkeypatch, tmp_path, capsys):
    ft = FakeTmux(server=False)  # nothing exists
    monkeypatch.setattr(cli, "tmuxio", ft)
    rc = cli.main(["kill", "backend"])
    assert rc == 1
    assert not any(c[0] == "kill_session" for c in ft.calls)
    assert "No session" in capsys.readouterr().out


def test_bare_positional_is_not_a_session_name(fake_env):
    # The verb scheme means a bare word is parsed as a (missing) subcommand,
    # so a mistyped command errors instead of silently creating a session.
    with pytest.raises(SystemExit):
        cli.main(["stauts"])


def test_slugify_session_sanitizes_illegal_chars():
    # tmux forbids `.` and `:` in session names; whitespace is awkward.
    assert cli._slugify_session("my.app") == "my-app"
    assert cli._slugify_session("foo bar:baz") == "foo-bar-baz"
    assert cli._slugify_session("  ") == "vupai"  # empty falls back


def test_default_reload_respawns_daemon_then_attaches(fake_env, monkeypatch):
    # `vupai --reload` collapses `reload && vupai`: kill the running daemon,
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
    assert ("attach", cli._resolve_session_name(None)) in ft.calls


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
    assert not any(c[0] == "attach" for c in ft.calls)  # but no same-server nesting
    assert "staying" in capsys.readouterr().out.lower()


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


def _pane(name, pane_id="%2", *, session="", active=True):
    from vupai.registry import Pane
    return Pane(id=pane_id, window_id="@1", window="w", index=1,
                name=name, command="claude", active=active, session=session)


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
    from vupai.router import CALLSIGNS
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
    from vupai.router import CALLSIGNS
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [_pane("%7", "%7")])
    rc = cli.main(["autoname", "%7"])
    assert rc == 0
    assert ("set_pane_name", "%7", CALLSIGNS[0]) in ft.calls


def test_autoname_avoids_callsign_already_in_use(fake_env, monkeypatch):
    from vupai.router import CALLSIGNS
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
    from vupai.permissions import PermissionStatus
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
    from vupai.permissions import PermissionStatus
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
    from vupai.permissions import PermissionStatus, TerminalApp
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
    from vupai.permissions import PermissionStatus, TerminalApp
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
    from vupai.permissions import PermissionStatus, TerminalApp
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
    from vupai.permissions import PermissionStatus, TerminalApp
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
    from vupai.permissions import PermissionStatus
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
    from vupai.permissions import PermissionStatus
    status = PermissionStatus(microphone=True, input_monitoring=True, accessibility=True)
    monkeypatch.setattr(cli, "missing_tools", lambda: [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: status)
    monkeypatch.setattr(cli, "model_cached", lambda mid: True)
    rc = cli.main(["doctor"])
    assert rc == 0
    assert "All checks passed." in capsys.readouterr().out


def test_status_reports_model_state(fake_env, monkeypatch, capsys):
    from vupai.permissions import PermissionStatus
    monkeypatch.setattr(cli, "daemon_state", lambda: "not_running")
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
    monkeypatch.setattr(cli, "_pid_is_vupai", lambda pid: True)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    rc = cli.main(["down"])
    assert rc == 0
    assert killed == [(4242, cli.signal.SIGTERM)]
    assert not pidfile.exists()


def test_down_skips_kill_for_non_vupai_pid(monkeypatch, tmp_path):
    # A stale pidfile whose PID the OS reassigned to an unrelated process must
    # NOT be SIGTERMed; vupai only signals a confirmed daemon. The stale file is
    # still cleared so the next `up` starts cleanly.
    ft = FakeTmux(server=True)
    monkeypatch.setattr(cli, "tmuxio", ft)
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("4242")
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    monkeypatch.setattr(cli, "_pid_is_vupai", lambda pid: False)
    killed: list = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    rc = cli.main(["down"])
    assert rc == 0
    assert killed == []                 # never signal a process that isn't ours
    assert not pidfile.exists()         # stale pidfile cleared


def test_pid_is_vupai_true_for_daemon_argv(monkeypatch):
    class _R:
        stdout = "/Users/x/.venv/bin/python -m vupai _daemon\n"
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _R())
    assert cli._pid_is_vupai(4242) is True


def test_pid_is_vupai_false_for_unrelated_process(monkeypatch):
    class _R:
        stdout = "/usr/bin/vim notes.txt\n"
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _R())
    assert cli._pid_is_vupai(4242) is False


def test_pid_is_vupai_false_when_ps_unavailable(monkeypatch):
    def _boom(*a, **k):
        raise OSError("ps missing")
    monkeypatch.setattr(cli.subprocess, "run", _boom)
    assert cli._pid_is_vupai(4242) is False


def test_daemon_running_false_when_pid_alive_but_not_vupai(monkeypatch, tmp_path):
    # os.kill(pid, 0) succeeds (PID exists) but it's a reused PID, not our daemon.
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("4242")
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(cli, "_pid_is_vupai", lambda pid: False)
    assert cli._daemon_running() is False


def test_daemon_running_true_for_live_vupai_pid(monkeypatch, tmp_path):
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("4242")
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(cli, "_pid_is_vupai", lambda pid: True)
    assert cli._daemon_running() is True


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
    monkeypatch.setattr(cli, "_pid_is_vupai", lambda pid: True)
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
    from vupai.permissions import PermissionStatus
    ft, pidfile = fake_env
    pidfile.write_text("999")
    monkeypatch.setattr(cli, "daemon_state", lambda: "ready")
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


def test_status_groups_by_session_and_marks_only_focused(fake_env, monkeypatch, capsys):
    # fake_env's tmux reports the focused pane as %1 (in repoA). Across two
    # sessions, only %1 gets `*`; each session's active pane gets `+`.
    ft, _ = fake_env
    monkeypatch.setattr(cli, "daemon_state", lambda: "ready")
    _stub_registry(monkeypatch, [
        _pane("nova", "%1", session="repoA", active=True),    # focused -> *
        _pane("atlas", "%3", session="repoA", active=False),  #          -> blank
        _pane("orion", "%2", session="repoB", active=True),   # repoB active -> +
        _pane("shell", "%4", session="repoB", active=False),  #              -> blank
    ])
    rc = cli.main(["status"])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    # Focused session is listed first and tagged.
    assert any(line.strip().startswith("repoA") and "(focused)" in line for line in lines)
    star = [ln for ln in lines if "%1" in ln][0]
    plus = [ln for ln in lines if "%2" in ln][0]
    blank = [ln for ln in lines if "%3" in ln][0]
    assert star.lstrip().startswith("*")          # voice target
    assert plus.lstrip().startswith("+")          # repoB's own active pane
    assert blank.lstrip().startswith("%3")        # no marker
    # repoA (focused) appears before repoB.
    assert lines.index(star) < lines.index(plus)


def test_daemon_builds_and_runs(monkeypatch, tmp_path):
    import os as _os
    monkeypatch.setattr(cli, "tmuxio", FakeTmux())
    pidfile = tmp_path / "daemon.pid"
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    monkeypatch.setattr(cli, "STATEFILE", tmp_path / "daemon.state")
    built = {}

    class FakeDaemon:
        def __init__(self, config, recorder, transcriber, registry, feedback,
                     *, route_fn=None, inject_fn=None, **kwargs):
            built["config"] = config
            built["transcriber"] = transcriber
            built["kwargs"] = kwargs
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


def test_daemon_installs_sigterm_handler_then_restores(monkeypatch, tmp_path):
    # `vupai down` SIGTERMs the daemon; without a handler the process dies
    # abruptly and run()'s teardown (which reaps sox) never runs. _cmd_daemon
    # must install a handler that calls daemon.stop(), then restore the prior one.
    monkeypatch.setattr(cli, "tmuxio", FakeTmux())
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "STATEFILE", tmp_path / "daemon.state")
    monkeypatch.setattr(cli, "load_config", lambda: __import__(
        "vupai.config", fromlist=["Config"]).Config())
    monkeypatch.setattr(cli.audio, "resolve_device", lambda d: ("", None))
    captured = {}
    stops = {"n": 0}

    class FakeDaemon:
        def __init__(self, *a, **k):
            ...

        def run(self):
            captured["handler"] = cli.signal.getsignal(cli.signal.SIGTERM)

        def stop(self):
            stops["n"] += 1

    monkeypatch.setattr(cli, "Daemon", FakeDaemon)
    monkeypatch.setattr(cli, "Recorder", lambda *a, **k: object())
    monkeypatch.setattr(cli, "ParakeetTranscriber", lambda model_id: object())
    monkeypatch.setattr(cli, "PaneRegistry", lambda *a, **k: object())
    monkeypatch.setattr(cli, "Feedback", lambda *a, **k: object())

    before = cli.signal.getsignal(cli.signal.SIGTERM)
    rc = cli.main(["_daemon"])
    assert rc == 0
    # A real handler was installed for the duration of run()...
    handler = captured["handler"]
    assert callable(handler) and handler is not before
    handler(cli.signal.SIGTERM, None)     # invoking it requests a clean shutdown
    assert stops["n"] == 1
    # ...and the prior handler is restored once run() returns.
    assert cli.signal.getsignal(cli.signal.SIGTERM) == before


# ---------------------------------------------------------------------------
# Gap 4: daemon state marker (warming / ready / crashed / stopped)
# ---------------------------------------------------------------------------

def test_write_daemon_state_format(tmp_path):
    sf = tmp_path / "daemon.state"
    cli.write_daemon_state("ready", pid=4321, statefile=sf, now=lambda: 1000.0)
    assert sf.read_text().strip() == "ready 4321 1000"


def test_write_daemon_state_overwrites_in_place(tmp_path):
    sf = tmp_path / "daemon.state"
    cli.write_daemon_state("starting", pid=1, statefile=sf, now=lambda: 1.0)
    cli.write_daemon_state("ready", pid=1, statefile=sf, now=lambda: 2.0)
    assert sf.read_text().strip() == "ready 1 2"


def test_daemon_state_not_running_when_no_pidfile(tmp_path):
    assert cli.daemon_state(
        pidfile=tmp_path / "no.pid", statefile=tmp_path / "no.state") == "not_running"


def test_daemon_state_warming_when_alive_and_marker_starting(tmp_path):
    pf = tmp_path / "p"
    pf.write_text("10")
    sf = tmp_path / "s"
    cli.write_daemon_state("starting", pid=10, statefile=sf, now=lambda: 1.0)
    assert cli.daemon_state(pidfile=pf, statefile=sf, liveness=lambda p: True) == "warming"


def test_daemon_state_ready_when_alive_and_marker_ready(tmp_path):
    pf = tmp_path / "p"
    pf.write_text("10")
    sf = tmp_path / "s"
    cli.write_daemon_state("ready", pid=10, statefile=sf, now=lambda: 1.0)
    assert cli.daemon_state(pidfile=pf, statefile=sf, liveness=lambda p: True) == "ready"


def test_daemon_state_crashed_when_dead_and_marker_ready(tmp_path):
    pf = tmp_path / "p"
    pf.write_text("10")
    sf = tmp_path / "s"
    cli.write_daemon_state("ready", pid=10, statefile=sf, now=lambda: 1.0)
    assert cli.daemon_state(pidfile=pf, statefile=sf, liveness=lambda p: False) == "crashed"


def test_daemon_state_crashed_when_dead_and_marker_starting(tmp_path):
    pf = tmp_path / "p"
    pf.write_text("10")
    sf = tmp_path / "s"
    cli.write_daemon_state("starting", pid=10, statefile=sf, now=lambda: 1.0)
    assert cli.daemon_state(pidfile=pf, statefile=sf, liveness=lambda p: False) == "crashed"


def test_daemon_state_stopped_when_dead_and_marker_stopped(tmp_path):
    pf = tmp_path / "p"
    pf.write_text("10")
    sf = tmp_path / "s"
    cli.write_daemon_state("stopped", pid=10, statefile=sf, now=lambda: 1.0)
    assert cli.daemon_state(pidfile=pf, statefile=sf, liveness=lambda p: False) == "stopped"


def test_daemon_state_ignores_marker_from_a_different_pid(tmp_path):
    # A stale marker left by an OLD daemon (marker pid != pidfile pid) must be
    # ignored, so a freshly-reused-but-dead pid isn't misread as that old crash.
    pf = tmp_path / "p"
    pf.write_text("10")
    sf = tmp_path / "s"
    cli.write_daemon_state("ready", pid=99, statefile=sf, now=lambda: 1.0)
    assert cli.daemon_state(pidfile=pf, statefile=sf, liveness=lambda p: False) == "not_running"


def _all_perms():
    from vupai.permissions import PermissionStatus
    return PermissionStatus(microphone=True, input_monitoring=True, accessibility=True)


def test_status_prints_warming(fake_env, monkeypatch, capsys):
    ft, pidfile = fake_env
    pidfile.write_text("4321")
    monkeypatch.setattr(cli, "daemon_state", lambda: "warming")
    _stub_registry(monkeypatch, [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: _all_perms())
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out.lower()
    assert "warming" in out and "4321" in out


def test_status_prints_crashed_with_log_hint(fake_env, monkeypatch, capsys):
    ft, pidfile = fake_env
    pidfile.write_text("4321")
    monkeypatch.setattr(cli, "daemon_state", lambda: "crashed")
    _stub_registry(monkeypatch, [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: _all_perms())
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out.lower()
    assert "crashed" in out and str(cli.DAEMON_LOG).lower() in out


def test_down_unlinks_statefile(monkeypatch, tmp_path):
    ft = FakeTmux(server=True)
    monkeypatch.setattr(cli, "tmuxio", ft)
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("4242")
    statefile = tmp_path / "daemon.state"
    statefile.write_text("ready 4242 1\n")
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    monkeypatch.setattr(cli, "STATEFILE", statefile)
    monkeypatch.setattr(cli, "_pid_is_vupai", lambda pid: True)
    monkeypatch.setattr(cli.os, "kill", lambda p, s: None)
    assert cli.main(["down"]) == 0
    assert not statefile.exists()


def test_cmd_daemon_writes_starting_marker_and_threads_state_writer(monkeypatch, tmp_path):
    from vupai.config import Config
    monkeypatch.setattr(cli, "tmuxio", FakeTmux())
    pidfile = tmp_path / "daemon.pid"
    statefile = tmp_path / "daemon.state"
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    monkeypatch.setattr(cli, "STATEFILE", statefile)
    monkeypatch.setattr(cli, "load_config", lambda: Config())
    monkeypatch.setattr(cli.audio, "resolve_device", lambda d: ("", None))
    built = {}

    class FakeDaemon:
        def __init__(self, *a, state_writer=None, **k):
            built["state_writer"] = state_writer

        def run(self):
            # The 'starting' marker must already be on disk by the time run starts.
            built["marker_at_run"] = statefile.read_text()

    monkeypatch.setattr(cli, "Daemon", FakeDaemon)
    monkeypatch.setattr(cli, "Recorder", lambda *a, **k: object())
    monkeypatch.setattr(cli, "ParakeetTranscriber", lambda model_id: object())
    monkeypatch.setattr(cli, "PaneRegistry", lambda *a, **k: object())
    monkeypatch.setattr(cli, "Feedback", lambda *a, **k: object())
    assert cli.main(["_daemon"]) == 0
    assert built["state_writer"] is not None
    assert "starting" in built["marker_at_run"]


def test_status_prints_notify_disabled_by_default(fake_env, monkeypatch, capsys):
    ft, pidfile = fake_env
    monkeypatch.setattr(cli, "daemon_state", lambda: "not_running")
    _stub_registry(monkeypatch, [])
    monkeypatch.setattr(cli, "check_permissions", lambda **k: _all_perms())
    assert cli.main(["status"]) == 0
    assert "notify: disabled" in capsys.readouterr().out


def test_cmd_daemon_builds_watcher_when_notify_enabled(monkeypatch, tmp_path):
    from vupai.config import Config
    monkeypatch.setattr(cli, "tmuxio", FakeTmux())
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "STATEFILE", tmp_path / "daemon.state")
    monkeypatch.setattr(cli, "load_config", lambda: Config(notify_enabled=True))
    monkeypatch.setattr(cli.audio, "resolve_device", lambda d: ("", None))
    built = {}

    class FakeDaemon:
        def __init__(self, *a, watcher=None, **k):
            built["watcher"] = watcher

        def run(self):
            ...

    monkeypatch.setattr(cli, "Daemon", FakeDaemon)
    monkeypatch.setattr(cli, "PaneWatcher", lambda *a, **k: ("watcher", k))
    monkeypatch.setattr(cli, "Recorder", lambda *a, **k: object())
    monkeypatch.setattr(cli, "ParakeetTranscriber", lambda model_id: object())
    monkeypatch.setattr(cli, "PaneRegistry", lambda *a, **k: object())
    monkeypatch.setattr(cli, "Feedback", lambda *a, **k: object())
    assert cli.main(["_daemon"]) == 0
    assert built["watcher"] is not None  # constructed and threaded into Daemon


def test_cmd_daemon_no_watcher_when_notify_disabled(monkeypatch, tmp_path):
    from vupai.config import Config
    monkeypatch.setattr(cli, "tmuxio", FakeTmux())
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "STATEFILE", tmp_path / "daemon.state")
    monkeypatch.setattr(cli, "load_config", lambda: Config(notify_enabled=False))
    monkeypatch.setattr(cli.audio, "resolve_device", lambda d: ("", None))
    built = {}

    class FakeDaemon:
        def __init__(self, *a, watcher=None, **k):
            built["watcher"] = watcher

        def run(self):
            ...

    monkeypatch.setattr(cli, "Daemon", FakeDaemon)
    monkeypatch.setattr(cli, "Recorder", lambda *a, **k: object())
    monkeypatch.setattr(cli, "ParakeetTranscriber", lambda model_id: object())
    monkeypatch.setattr(cli, "PaneRegistry", lambda *a, **k: object())
    monkeypatch.setattr(cli, "Feedback", lambda *a, **k: object())
    assert cli.main(["_daemon"]) == 0
    assert built["watcher"] is None


def test_cmd_daemon_threads_hud_flag(monkeypatch, tmp_path):
    from vupai.config import Config
    monkeypatch.setattr(cli, "tmuxio", FakeTmux())
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "STATEFILE", tmp_path / "daemon.state")
    monkeypatch.setattr(cli, "load_config", lambda: Config(hud_enabled=False))
    monkeypatch.setattr(cli.audio, "resolve_device", lambda d: ("", None))
    captured = {}

    def fake_feedback(*a, **k):
        captured.update(k)
        return object()

    class FakeDaemon:
        def __init__(self, *a, **k):
            ...

        def run(self):
            ...

    monkeypatch.setattr(cli, "Feedback", fake_feedback)
    monkeypatch.setattr(cli, "Daemon", FakeDaemon)
    monkeypatch.setattr(cli, "Recorder", lambda *a, **k: object())
    monkeypatch.setattr(cli, "ParakeetTranscriber", lambda model_id: object())
    monkeypatch.setattr(cli, "PaneRegistry", lambda *a, **k: object())
    assert cli.main(["_daemon"]) == 0
    assert captured.get("hud_enabled") is False


# ---------------------------------------------------------------------------
# voice-commands subcommand
# ---------------------------------------------------------------------------

def test_voice_commands_text_lists_verbs_and_words():
    from vupai.commands import _CLOSE_VERBS, _CREATE_VERBS
    from vupai.config import Config
    text = cli._voice_commands_text(Config())  # button is the default mode
    # Every create/close verb the parser accepts must appear in the cheat sheet.
    for verb in (*_CREATE_VERBS, *_CLOSE_VERBS):
        assert verb in text
    assert "focus" in text and "swap" in text
    assert "layout" in text  # layout command is documented
    # Config-driven broadcast word.
    assert "everyone" in text


def test_voice_commands_text_keyword_mode_has_no_command_layer():
    from vupai.config import Config
    text = cli._voice_commands_text(Config(addressing="keyword"))
    assert "keyword" in text
    assert "no command layer" in text       # commands live on the button system key
    assert "create <n> panes" not in text   # no command table in keyword mode


def test_voice_commands_text_button_mode_shows_both_keys():
    from vupai.config import Config
    cfg = Config(addressing="button", hotkey="alt_r", command_hotkey="alt_l")
    text = cli._voice_commands_text(cfg)
    assert "button" in text
    assert "alt_l" in text and "alt_r" in text
    assert "computer create" not in text  # no control-word prefix in button mode


def test_voice_commands_text_lists_configured_macros():
    from vupai.config import Config
    cfg = Config(macros={"set up": ["create two panes", "tile"]})
    text = cli._voice_commands_text(cfg)
    assert "set up" in text
    assert "create two panes" in text


def test_voice_commands_prints(fake_env, monkeypatch, capsys):
    from vupai.config import Config
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: Config())
    rc = cli.main(["voice-commands"])
    assert rc == 0
    assert "voice commands" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Parser coverage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    [], ["up"], ["down"], ["reload"], ["cleanup"], ["status"], ["doctor"],
    ["voice-commands"],
    ["name", "x"], ["name", "x", "%3"], ["autoname"], ["autoname", "%3"],
    ["keys"], ["board"], ["_daemon"], ["_board"],
])
def test_parser_accepts_all_subcommands(argv):
    parser = cli.build_parser()
    ns = parser.parse_args(argv)
    assert callable(ns.func)


def test_cleanup_reverts_default_server(fake_env, capsys):
    # `vupai cleanup` reverts vupai's globals on the user's DEFAULT tmux server.
    ft, _ = fake_env
    rc = cli.main(["cleanup"])
    assert rc == 0
    assert ("cleanup_default_server",) in ft.calls
    assert "default tmux server" in capsys.readouterr().out


def test_seed_socket_defaults_to_config(monkeypatch):
    # No env var: the dedicated socket comes from config (default "vupai") and is
    # exported so the daemon and tmux children inherit it.
    monkeypatch.delenv("VTMUX_TMUX_SOCKET", raising=False)
    cli._seed_tmux_socket()
    assert os.environ["VTMUX_TMUX_SOCKET"] == "vupai"


def test_seed_socket_env_wins_over_config(monkeypatch):
    # An explicit env var (tests, power users) is never clobbered by config.
    monkeypatch.setenv("VTMUX_TMUX_SOCKET", "itest-sock")
    cli._seed_tmux_socket()
    assert os.environ["VTMUX_TMUX_SOCKET"] == "itest-sock"


def test_seed_socket_rejects_unsafe_name(monkeypatch, capsys):
    # A socket name with shell metacharacters would mis-target via run-shell;
    # fall back to the safe default instead.
    monkeypatch.setenv("VTMUX_TMUX_SOCKET", "x$(boom)")
    cli._seed_tmux_socket()
    assert os.environ["VTMUX_TMUX_SOCKET"] == "vupai"
    assert "Invalid tmux_socket" in capsys.readouterr().out


def test_seed_socket_empty_config_shares_default_server(monkeypatch):
    # tmux_socket="" is the opt-out: no socket exported => shared default server.
    from vupai.config import Config
    monkeypatch.delenv("VTMUX_TMUX_SOCKET", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda: Config(tmux_socket=""))
    cli._seed_tmux_socket()
    assert "VTMUX_TMUX_SOCKET" not in os.environ


def test_daemon_subcommand_is_hidden_in_help():
    parser = cli.build_parser()
    help_text = parser.format_help()
    assert "_daemon" not in help_text   # hidden, though still parseable (above)
    assert "_board" not in help_text    # hidden too
    assert "board" in help_text         # but the public `board` is listed


def test_board_opens_pane_against_focused(fake_env):
    ft, _ = fake_env
    rc = cli.main(["board"])
    assert rc == 0
    splits = [c for c in ft.calls if c[0] == "split_window"]
    assert len(splits) == 1
    target, program, horizontal, size = splits[0][1:]
    assert target == "%1"               # split the focused pane
    assert program.endswith("_board")   # launches the hidden render loop
    assert horizontal is True and size == "40%"
    assert ("mark_board_pane", "%7") in ft.calls
    assert ("set_pane_name", "%7", "board") in ft.calls


def test_board_errors_without_focused_pane(fake_env, monkeypatch, capsys):
    ft, _ = fake_env
    monkeypatch.setattr(ft, "_focused", None)   # focused_pane_id() -> None
    rc = cli.main(["board"])
    assert rc == 1
    assert "vupai up" in capsys.readouterr().out


def test_board_does_not_open_second_board_in_session(fake_env, monkeypatch, capsys):
    ft, _ = fake_env
    monkeypatch.setattr(ft, "_board_pane", "%5")   # a board already exists
    rc = cli.main(["board"])
    assert rc == 0
    assert [c for c in ft.calls if c[0] == "split_window"] == []  # no second split
    assert ("select_pane", "%5") in ft.calls                      # focuses the existing one
    assert "already open" in capsys.readouterr().out


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
    from vupai.permissions import PermissionStatus, TerminalApp
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
    # Extra prompts (the hotkey step) get "" -> keep current.
    monkeypatch.setattr("builtins.input", lambda _: next(answers, ""))
    rc = cli.main(["setup"])
    assert rc == 0
    written = cli.load_config(cfg)
    assert written.journal_enabled is True
    assert written.journal_keep_audio is False


# ---------------------------------------------------------------------------
# mic command + setup mic step
# ---------------------------------------------------------------------------

def _devs():
    from vupai.audio import InputDevice
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


def test_mic_select_refuses_unusable_device(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    monkeypatch.setattr(
        cli.audio, "probe_capture",
        lambda name, **k: "cannot record from 'AirPods Pro': name collision")
    wrote = []
    monkeypatch.setattr(cli, "set_mic_device", lambda name: wrote.append(name))
    rc = cli.main(["mic", "1"])
    assert rc == 1
    assert wrote == []  # not pinned
    out = capsys.readouterr().out
    assert "Cannot use 'AirPods Pro'" in out
    assert "--force" in out


def test_mic_force_pins_despite_probe_failure(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    probed = []
    monkeypatch.setattr(
        cli.audio, "probe_capture",
        lambda name, **k: probed.append(name) or "broken")
    saved = {}
    monkeypatch.setattr(
        cli, "set_mic_device", lambda name: saved.setdefault("name", name))
    rc = cli.main(["mic", "1", "--force"])
    assert rc == 0
    assert saved["name"] == "AirPods Pro"
    assert probed == []  # --force skips the probe entirely


def test_mic_default_skips_probe(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.audio, "list_input_devices", lambda **k: _devs())
    probed = []
    monkeypatch.setattr(
        cli.audio, "probe_capture",
        lambda name, **k: probed.append(name) or "should not be called")
    monkeypatch.setattr(cli, "set_mic_device", lambda name: None)
    rc = cli.main(["mic", "default"])
    assert rc == 0
    assert probed == []  # clearing the pin never probes


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


# ---------------------------------------------------------------------------
# Hotkey / trigger-key setup
# ---------------------------------------------------------------------------

def _reader(values):
    """A reader callable that returns queued answers, ignoring the prompt."""
    it = iter(values)
    return lambda _prompt="": next(it)


def _capture_writes(monkeypatch):
    """Capture set_hotkey_config kwargs. With config_path pointed at a missing
    file the current config is all defaults (button/alt_r/cmd_r)."""
    saved = {}
    monkeypatch.setattr(
        cli, "set_hotkey_config",
        lambda *, path=None, **kw: saved.update(kw))
    return saved


def test_keys_prompt_menu_index_button(fake_env, monkeypatch, tmp_path):
    saved = _capture_writes(monkeypatch)
    cfgpath = tmp_path / "missing.toml"
    # addressing: keep (button); dictation idx 4 -> ctrl_r; command idx 2 -> cmd_r
    cli._prompt_hotkey_setup(reader=_reader(["", "4", "2"]), config_path=cfgpath)
    assert saved == {
        "addressing": "button", "hotkey": "ctrl_r", "command_hotkey": "cmd_r"}


def test_keys_prompt_exact_name(fake_env, monkeypatch, tmp_path):
    saved = _capture_writes(monkeypatch)
    cfgpath = tmp_path / "missing.toml"
    cli._prompt_hotkey_setup(
        reader=_reader(["", "f13", "f14"]), config_path=cfgpath)
    assert saved["hotkey"] == "f13"
    assert saved["command_hotkey"] == "f14"


def test_keys_prompt_capture_press_a_key(fake_env, monkeypatch, tmp_path):
    saved = _capture_writes(monkeypatch)
    cfgpath = tmp_path / "missing.toml"
    captures = iter(["ctrl_r", "cmd_r"])
    cli._prompt_hotkey_setup(
        reader=_reader(["", "p", "p"]),
        capture=lambda *a, **k: next(captures),
        config_path=cfgpath)
    assert saved["hotkey"] == "ctrl_r"
    assert saved["command_hotkey"] == "cmd_r"


def test_keys_prompt_bare_enter_keeps_current(fake_env, monkeypatch, tmp_path):
    wrote = []
    monkeypatch.setattr(
        cli, "set_hotkey_config", lambda *, path=None, **kw: wrote.append(kw))
    cfgpath = tmp_path / "missing.toml"  # defaults: button/alt_r/cmd_r
    cli._prompt_hotkey_setup(
        reader=_reader(["", "", ""]), config_path=cfgpath)
    assert wrote == []  # nothing changed -> no write


def test_keys_prompt_invalid_then_valid(fake_env, monkeypatch, tmp_path):
    saved = _capture_writes(monkeypatch)
    cfgpath = tmp_path / "missing.toml"
    # dictation: junk then idx 4 (ctrl_r); command: idx 2 (cmd_r)
    cli._prompt_hotkey_setup(
        reader=_reader(["", "nope", "4", "2"]), config_path=cfgpath)
    assert saved["hotkey"] == "ctrl_r"
    assert saved["command_hotkey"] == "cmd_r"


def test_keys_prompt_collision_reasks_command(fake_env, monkeypatch, tmp_path):
    saved = _capture_writes(monkeypatch)
    cfgpath = tmp_path / "missing.toml"
    # dictation idx 4 (ctrl_r); command idx 4 (ctrl_r, collides) then idx 2 (cmd_r)
    cli._prompt_hotkey_setup(
        reader=_reader(["", "4", "4", "2"]), config_path=cfgpath)
    assert saved["hotkey"] == "ctrl_r"
    assert saved["command_hotkey"] == "cmd_r"


def test_keys_prompt_keyword_mode(fake_env, monkeypatch, tmp_path):
    saved = _capture_writes(monkeypatch)
    cfgpath = tmp_path / "missing.toml"
    # addressing 2 -> keyword; dictation idx 4 -> ctrl_r; no command prompt
    cli._prompt_hotkey_setup(
        reader=_reader(["2", "4"]), config_path=cfgpath)
    assert saved["addressing"] == "keyword"
    assert saved["hotkey"] == "ctrl_r"
    # command key preserved from current config (default cmd_r)
    assert saved["command_hotkey"] == "cmd_r"


def test_cmd_keys_prints_current_then_prompts(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_prompt_hotkey_setup", lambda: None)
    ns = cli.build_parser().parse_args(["keys"])
    rc = cli._cmd_keys(ns)
    assert rc == 0
    out = capsys.readouterr().out
    assert "button" in out
    assert "alt_r" in out


# ---------------------------------------------------------------------------
# vupai config --init
# ---------------------------------------------------------------------------

def test_cmd_config_init_writes_template(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from vupai.config import Config, load_config

    p = tmp_path / "config.toml"
    monkeypatch.setattr(cli, "CONFIG_PATH", p)
    assert cli._cmd_config(SimpleNamespace(init=True)) == 0
    assert load_config(p) == Config()
    out = capsys.readouterr().out
    assert str(p) in out and "Wrote" in out


def test_cmd_config_init_appends_missing_without_backup(
    tmp_path, monkeypatch, capsys
):
    from types import SimpleNamespace

    from vupai.config import load_config

    p = tmp_path / "config.toml"
    p.write_text("hotkey = \"f13\"\n", encoding="utf-8")
    monkeypatch.setattr(cli, "CONFIG_PATH", p)
    assert cli._cmd_config(SimpleNamespace(init=True)) == 0
    out = capsys.readouterr().out
    # additive: no backup file is created, the chosen value is preserved, and
    # the missing keys are reported as added
    assert not (tmp_path / "config.toml.bak").exists()
    assert "Added" in out and "journal_enabled" in out
    assert load_config(p).hotkey == "f13"


def test_cmd_config_init_noop_when_complete(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from vupai.config import write_full_config

    p = tmp_path / "config.toml"
    write_full_config(journal_enabled=True, journal_keep_audio=False, path=p)
    monkeypatch.setattr(cli, "CONFIG_PATH", p)
    assert cli._cmd_config(SimpleNamespace(init=True)) == 0
    assert "already lists every key" in capsys.readouterr().out
