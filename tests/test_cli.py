import sys

import pytest

from voxpane import cli
from voxpane.tmuxio import TmuxError


class FakeTmux:
    """In-memory stand-in for the tmuxio module."""

    def __init__(self, *, server=True, windows=None, focused="%1",
                 kill_window_raises=False):
        self._server = server
        self.windows = set(windows or [])
        self._focused = focused
        self._kill_window_raises = kill_window_raises
        self.calls: list[tuple] = []
        self.daemon_spawns: list = []

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

    def kill_window(self, name: str) -> None:
        self.calls.append(("kill_window", name))
        if self._kill_window_raises:
            raise TmuxError("no window named voice")
        self.windows.discard(name)

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
    ft = FakeTmux(server=True, windows=set(), focused="%1")
    monkeypatch.setattr(cli, "tmuxio", ft)
    pidfile = tmp_path / "daemon.pid"
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
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
    assert not any(c[0] == "new_window" for c in ft.calls)
    assert ("enable_pane_titles",) in ft.calls


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
    ft = FakeTmux(server=False, windows=set())
    monkeypatch.setattr(cli, "tmuxio", ft)
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(cli, "_daemon_running", lambda: False)
    monkeypatch.setattr(cli, "_spawn_daemon", lambda: None)
    _stub_registry(monkeypatch, [])  # ensure_up sweeps the registry; keep it hermetic
    rc = cli.main(["up"])
    assert rc == 0
    # new-session issued via tmuxio.run WITHOUT a redundant leading "tmux"
    run_calls = [c for c in ft.calls if c[0] == "run"]
    assert ["new-session", "-d", "-s", "voxpane"] in [list(c[1]) for c in run_calls]
    assert all(c[1][0] != "tmux" for c in run_calls)  # run() prepends tmux itself


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


def test_name_rejects_control_word(fake_env, monkeypatch, capsys):
    ft, pidfile = fake_env
    _stub_registry(monkeypatch, [])
    rc = cli.main(["name", "computer"])
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
    # Fix 2: voice window must be killed so `up` can restart the daemon.
    assert ("kill_window", "voice") in ft.calls
    assert not pidfile.exists()


def test_down_with_pidfile_survives_kill_window_error(monkeypatch, tmp_path):
    # pidfile present; kill_window raises TmuxError (window gone); down must not crash.
    ft = FakeTmux(server=True, kill_window_raises=True)
    monkeypatch.setattr(cli, "tmuxio", ft)
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("9999")
    monkeypatch.setattr(cli, "PIDFILE", pidfile)
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
    rc = cli.main(["down"])
    assert rc == 0
    assert not pidfile.exists()


def test_down_kills_orphaned_window_without_pidfile(monkeypatch, tmp_path):
    # No pidfile (daemon crashed before writing it, or it was removed), but the
    # voice window may still be alive: down must still tear it down so a later
    # `up` can recreate the daemon.
    ft = FakeTmux(server=True)
    monkeypatch.setattr(cli, "tmuxio", ft)
    monkeypatch.setattr(cli, "PIDFILE", tmp_path / "missing.pid")
    killed: list = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    rc = cli.main(["down"])
    assert rc == 0
    assert killed == []                        # no pid -> no os.kill
    assert ("kill_window", "voice") in ft.calls


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
    text = cli._voice_commands_text(Config())
    # Every create/close verb the parser accepts must appear in the cheat sheet.
    for verb in (*_CREATE_VERBS, *_CLOSE_VERBS):
        assert verb in text
    assert "focus" in text and "swap" in text
    # Config-driven words.
    assert "computer" in text and "everyone" in text


def test_voice_commands_text_keyword_mode_prefixes_control_word():
    from voxpane.config import Config
    text = cli._voice_commands_text(Config(addressing="keyword"))
    assert "keyword" in text
    assert "computer create" in text  # commands are prefixed in keyword mode


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
    [], ["up"], ["down"], ["status"], ["doctor"], ["voice-commands"],
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
