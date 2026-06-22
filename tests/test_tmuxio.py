import subprocess
from dataclasses import dataclass, field

import pytest

from vupai import tmuxio


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
    fake = FakeRun(stdout="%1\t@1\twin\t0\tname\tzsh\t1\trepo\n%2\t@1\twin\t1\tn2\tnode\t0\trepo\n")
    patch_run(monkeypatch, fake)
    lines = tmuxio.list_panes()
    assert fake.calls[0]["args"] == ["tmux", "list-panes", "-a", "-F", tmuxio.PANE_FORMAT]
    assert lines == ["%1\t@1\twin\t0\tname\tzsh\t1\trepo", "%2\t@1\twin\t1\tn2\tnode\t0\trepo"]


def test_list_panes_ignores_blank_lines(monkeypatch):
    fake = FakeRun(stdout="%1\t@1\twin\t0\tname\tzsh\t1\trepo\n\n")
    patch_run(monkeypatch, fake)
    assert tmuxio.list_panes() == ["%1\t@1\twin\t0\tname\tzsh\t1\trepo"]


@dataclass
class VerbRun:
    """Returns canned CompletedProcesses keyed by the first tmux subcommand."""
    responses: dict
    calls: list[dict] = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        verb = args[1] if len(args) > 1 else ""
        rc, out, err = self.responses.get(verb, (0, "", ""))
        return subprocess.CompletedProcess(
            args=args, returncode=rc, stdout=out, stderr=err)


def test_focused_pane_id_targets_most_recent_attached_client(monkeypatch):
    # Two attached clients on different sessions; the newer activity wins, and
    # the active pane is read in that client's context (-c).
    fake = VerbRun({
        "list-clients": (0, "100\t/dev/ttys001\n200\t/dev/ttys002\n", ""),
        "display-message": (0, "%9\n", ""),
    })
    patch_run(monkeypatch, fake)
    assert tmuxio.focused_pane_id() == "%9"
    dm = [c["args"] for c in fake.calls if c["args"][1] == "display-message"][0]
    assert dm == ["tmux", "display-message", "-c", "/dev/ttys002", "-p", "#{pane_id}"]


def test_focused_pane_id_falls_back_to_bare_query_when_no_clients(monkeypatch):
    fake = VerbRun({
        "list-clients": (0, "", ""),
        "display-message": (0, "%7\n", ""),
    })
    patch_run(monkeypatch, fake)
    assert tmuxio.focused_pane_id() == "%7"
    dm = [c["args"] for c in fake.calls if c["args"][1] == "display-message"][0]
    assert dm == ["tmux", "display-message", "-p", "#{pane_id}"]


def test_focused_pane_id_falls_back_when_list_clients_errors(monkeypatch):
    fake = VerbRun({
        "list-clients": (1, "", "no server running"),
        "display-message": (0, "%3\n", ""),
    })
    patch_run(monkeypatch, fake)
    assert tmuxio.focused_pane_id() == "%3"
    dm = [c["args"] for c in fake.calls if c["args"][1] == "display-message"][0]
    assert dm == ["tmux", "display-message", "-p", "#{pane_id}"]


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
    assert fake.calls[0]["args"] == ["tmux", "capture-pane", "-J", "-p", "-t", "%3"]


def test_send_enter_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.send_enter("%3")
    assert fake.calls[0]["args"] == ["tmux", "send-keys", "-t", "%3", "Enter"]


def test_set_pane_name_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.set_pane_name("%3", "backend")
    # Stored in a per-pane user option, not the app-owned pane title.
    assert fake.calls[0]["args"] == [
        "tmux", "set", "-p", "-t", "%3", "@vupai_name", "backend",
    ]


def test_set_pane_program_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.set_pane_program("%3", "claude")
    # Stored in a per-pane user option so the agent can't clobber it (pane_title
    # would be overwritten by the agent's own summary).
    assert fake.calls[0]["args"] == [
        "tmux", "set", "-p", "-t", "%3", "@vupai_program", "claude",
    ]


def test_enable_pane_titles_runs_both_set_commands(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.enable_pane_titles()
    assert fake.calls[0]["args"] == ["tmux", "set", "-g", "pane-border-status", "top"]
    # name · program · pane_title, each segment conditional so it collapses when
    # its option is unset.
    assert fake.calls[1]["args"] == [
        "tmux", "set", "-g", "pane-border-format",
        "#{?@vupai_name,#[bold]#{@vupai_name}#[nobold] · ,}"
        "#{?@vupai_program,#{@vupai_program} · ,}"
        "#{pane_title}",
    ]


def test_set_terminal_title_enables_session_aware_title(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.set_terminal_title()
    assert fake.calls[0]["args"] == ["tmux", "set", "-g", "set-titles", "on"]
    # #S expands to the attached session, so each session's tab is distinct.
    assert fake.calls[1]["args"] == [
        "tmux", "set", "-g", "set-titles-string", "vupai - #S"]


def test_set_base_index_makes_windows_and_panes_one_based(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.set_base_index()
    assert fake.calls[0]["args"] == ["tmux", "set", "-g", "base-index", "1"]
    assert fake.calls[1]["args"] == ["tmux", "set", "-g", "pane-base-index", "1"]


def test_set_status_sets_option_then_refreshes(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.set_status("🎙 listening")
    assert fake.calls[0]["args"] == ["tmux", "set", "-g", "@vupai_status", "🎙 listening"]
    assert fake.calls[1]["args"] == ["tmux", "refresh-client", "-S"]


def test_set_status_swallows_refresh_failure_without_client(monkeypatch):
    # `refresh-client` fails when no client is attached; the option still sets.
    class RefreshFails:
        def __init__(self):
            self.calls = []

        def __call__(self, args, **kwargs):
            self.calls.append({"args": args, "kwargs": kwargs})
            failed = args[1] == "refresh-client"
            return subprocess.CompletedProcess(
                args=args, returncode=1 if failed else 0,
                stdout="", stderr="no current client" if failed else "")

    fake = RefreshFails()
    patch_run(monkeypatch, fake)
    tmuxio.set_status("x")  # must not raise
    assert fake.calls[0]["args"][1] == "set"
    assert fake.calls[1]["args"][1] == "refresh-client"


_UNSET = object()


class ScriptedRun:
    """subprocess.run stand-in that answers `show -gv <opt>` from a dict (missing
    key => unset => exit 1, like tmux) and records every call. `set` calls are
    recorded and return success; for read-after-write tests, set mutates the dict."""

    def __init__(self, options=None):
        self.options = dict(options or {})
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if "show" in args and "-gv" in args:
            opt = args[-1]
            val = self.options.get(opt, _UNSET)
            if val is _UNSET:
                return subprocess.CompletedProcess(
                    args, 1, "", f"invalid option: {opt}")
            return subprocess.CompletedProcess(args, 0, val + "\n", "")
        if args[1] == "set" and args[2] == "-g":  # plain set (not -gu unset)
            self.options[args[-2]] = args[-1]
        return subprocess.CompletedProcess(args, 0, "", "")

    def set_values(self):
        """Return the plain `-g` set calls as (option, value) pairs (no unsets)."""
        out = []
        for c in self.calls:
            a = c["args"]
            if a[1] == "set" and a[2] == "-g":
                out.append((a[-2], a[-1]))
        return out


def test_install_status_indicator_fresh_prepends_to_existing(monkeypatch):
    # First install on a server with a user's custom status-right.
    fake = ScriptedRun({"status-right": "MYRIGHT %H:%M", "status-right-length": "40"})
    patch_run(monkeypatch, fake)
    tmuxio.install_status_indicator()
    sets = dict(fake.set_values())
    assert sets["@vupai_status"] == "#[fg=green]● vupai#[default]"
    assert sets["@vupai_status_orig"] == "MYRIGHT %H:%M"   # captured original
    assert sets["status-right"] == "#{@vupai_status}  MYRIGHT %H:%M"  # prepended
    assert sets["status-right-length"] == "120"               # grown from 40


def test_install_is_idempotent_uses_saved_original(monkeypatch):
    # Re-install: saved original exists, live status-right already has the segment.
    fake = ScriptedRun({
        "@vupai_status_orig": "MYRIGHT %H:%M",
        "status-right": "#{@vupai_status}  MYRIGHT %H:%M",
        "status-right-length": "120",
    })
    patch_run(monkeypatch, fake)
    tmuxio.install_status_indicator()
    sets = dict(fake.set_values())
    # Rebuilt from the SAVED original, not the live (already-prepended) value.
    assert sets["status-right"] == "#{@vupai_status}  MYRIGHT %H:%M"
    # Original not re-captured (no stacking).
    assert "@vupai_status_orig" not in sets
    # Length already >= 120: left untouched.
    assert "status-right-length" not in sets


def test_install_recovery_when_segment_present_without_saved_original(monkeypatch):
    # The clobber case this change fixes: status-right already has our segment but
    # nothing was saved. Must NOT capture our own segment as the original.
    fake = ScriptedRun({"status-right": "#{@vupai_status}  %H:%M ",
                        "status-right-length": "120"})
    patch_run(monkeypatch, fake)
    tmuxio.install_status_indicator()
    sets = dict(fake.set_values())
    assert sets["@vupai_status_orig"] == ""            # captured as empty
    assert sets["status-right"] == "#{@vupai_status}  %H:%M "  # clock fallback


def test_install_does_not_shrink_existing_length(monkeypatch):
    fake = ScriptedRun({"status-right": "x", "status-right-length": "200"})
    patch_run(monkeypatch, fake)
    tmuxio.install_status_indicator()
    assert "status-right-length" not in dict(fake.set_values())


def test_restore_status_right_puts_back_saved_original(monkeypatch):
    fake = ScriptedRun({"@vupai_status_orig": "MYRIGHT %H:%M"})
    patch_run(monkeypatch, fake)
    tmuxio.restore_status_right()
    set_calls = [c["args"] for c in fake.calls if c["args"][1] == "set"]
    assert ["tmux", "set", "-g", "status-right", "MYRIGHT %H:%M"] in set_calls
    assert ["tmux", "set", "-gu", "@vupai_status_orig"] in set_calls
    assert ["tmux", "set", "-gu", "@vupai_status"] in set_calls


def test_restore_status_right_unsets_when_original_was_empty(monkeypatch):
    fake = ScriptedRun({"@vupai_status_orig": ""})
    patch_run(monkeypatch, fake)
    tmuxio.restore_status_right()
    set_calls = [c["args"] for c in fake.calls if c["args"][1] == "set"]
    assert ["tmux", "set", "-gu", "status-right"] in set_calls  # back to default
    assert ["tmux", "set", "-gu", "@vupai_status_orig"] in set_calls


def test_restore_status_right_safe_when_nothing_installed(monkeypatch):
    fake = ScriptedRun({})  # nothing saved
    patch_run(monkeypatch, fake)
    tmuxio.restore_status_right()
    set_calls = [c["args"] for c in fake.calls if c["args"][1] == "set"]
    # Only the @vupai_status unset; status-right is left alone.
    assert ["tmux", "set", "-gu", "@vupai_status"] in set_calls
    assert all("status-right" not in a for a in set_calls)


def test_show_global_returns_none_when_unset(monkeypatch):
    fake = ScriptedRun({})
    patch_run(monkeypatch, fake)
    assert tmuxio.show_global("@nope") is None


def test_show_global_returns_value_including_empty(monkeypatch):
    fake = ScriptedRun({"@e": "", "@v": "hi"})
    patch_run(monkeypatch, fake)
    assert tmuxio.show_global("@e") == ""    # set-empty is distinct from unset
    assert tmuxio.show_global("@v") == "hi"


def test_set_pane_autoname_hooks_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.set_pane_autoname_hooks("PY -m vupai")
    expected = 'run-shell "PY -m vupai autoname #{pane_id} >/dev/null 2>&1"'
    assert fake.calls[0]["args"] == ["tmux", "set-hook", "-g", "after-split-window", expected]
    assert fake.calls[1]["args"] == ["tmux", "set-hook", "-g", "after-new-window", expected]


def test_bind_rename_key_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.bind_rename_key("PY -m vupai")
    assert fake.calls[0]["args"] == [
        "tmux", "bind-key", "R", "command-prompt", "-p", "rename pane:",
        "run-shell \"PY -m vupai name '%%' #{pane_id}\"",
    ]


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


def test_split_window_argv_returns_pane_id(monkeypatch):
    fake = FakeRun(stdout="%9\n")
    patch_run(monkeypatch, fake)
    new_id = tmuxio.split_window("@1", "claude")
    assert new_id == "%9"
    assert fake.calls[0]["args"] == [
        "tmux", "split-window", "-P", "-F", "#{pane_id}", "-t", "@1", "claude",
    ]


def test_split_window_empty_program_omits_arg(monkeypatch):
    fake = FakeRun(stdout="%9\n")
    patch_run(monkeypatch, fake)
    tmuxio.split_window("@1", "")
    assert fake.calls[0]["args"] == [
        "tmux", "split-window", "-P", "-F", "#{pane_id}", "-t", "@1",
    ]


def test_select_layout_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.select_layout("@1", "tiled")
    assert fake.calls[0]["args"] == ["tmux", "select-layout", "-t", "@1", "tiled"]


def test_kill_pane_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.kill_pane("%3")
    assert fake.calls[0]["args"] == ["tmux", "kill-pane", "-t", "%3"]


def test_select_pane_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.select_pane("%3")
    assert fake.calls[0]["args"] == ["tmux", "select-pane", "-t", "%3"]


def test_swap_pane_argv(monkeypatch):
    fake = FakeRun()
    patch_run(monkeypatch, fake)
    tmuxio.swap_pane("%1", "%2")
    assert fake.calls[0]["args"] == ["tmux", "swap-pane", "-s", "%1", "-t", "%2"]


def test_attach_execs_tmux_attach(monkeypatch):
    captured = {}

    def fake_execvp(file, args):
        captured["file"] = file
        captured["args"] = args

    monkeypatch.setattr(tmuxio.os, "execvp", fake_execvp)
    tmuxio.attach()
    assert captured["file"] == "tmux"
    assert captured["args"] == ["tmux", "attach"]


def test_inside_tmux_reflects_env(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1234,0")
    assert tmuxio.inside_tmux() is True
    monkeypatch.delenv("TMUX", raising=False)
    assert tmuxio.inside_tmux() is False


def test_set_tip_sets_option_then_refreshes(monkeypatch):
    fake = ScriptedRun()
    patch_run(monkeypatch, fake)
    tmuxio.set_tip("tip: focus nova")
    assert fake.calls[0]["args"] == ["tmux", "set", "-g", "@vupai_tip", "tip: focus nova"]
    assert fake.calls[1]["args"] == ["tmux", "refresh-client", "-S"]


def test_install_tip_segment_appends_to_existing_left(monkeypatch):
    fake = ScriptedRun({"status-left": "[#S] ", "status-left-length": "10"})
    patch_run(monkeypatch, fake)
    tmuxio.install_tip_segment()
    sets = dict(fake.set_values())
    assert sets["@vupai_tip_orig"] == "[#S] "          # captured original
    # original + tip after it, with a trailing gap so the tip never butts
    # against tmux's window list (drawn right after status-left).
    assert sets["status-left"] == "[#S]   #{@vupai_tip}  "
    assert sets["status-left-length"] == "80"            # grown from 10


def test_install_tip_segment_is_idempotent(monkeypatch):
    fake = ScriptedRun({
        "@vupai_tip_orig": "[#S] ",
        "status-left": "[#S]   #{@vupai_tip}  ",
        "status-left-length": "80",
    })
    patch_run(monkeypatch, fake)
    tmuxio.install_tip_segment()
    sets = dict(fake.set_values())
    assert sets["status-left"] == "[#S]   #{@vupai_tip}  "  # segment never stacks
    assert "@vupai_tip_orig" not in sets                  # not recaptured
    assert "status-left-length" not in sets               # already >= 80


def test_restore_status_left_puts_original_back_and_drops_options(monkeypatch):
    fake = ScriptedRun({"@vupai_tip_orig": "[#S] "})
    patch_run(monkeypatch, fake)
    tmuxio.restore_status_left()
    args = [c["args"] for c in fake.calls]
    assert ["tmux", "set", "-g", "status-left", "[#S] "] in args
    assert ["tmux", "set", "-gu", "@vupai_tip_orig"] in args
    assert ["tmux", "set", "-gu", "@vupai_tip"] in args


@pytest.mark.integration
def test_list_panes_roundtrip_real_tmux():
    # Uses a throwaway, isolated tmux server via a private socket name so it
    # cannot touch the user's running tmux.
    import os
    import subprocess as sp

    socket = "vupai-itest"
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
