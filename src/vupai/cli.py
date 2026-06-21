"""vupai CLI entry point — subcommand dispatch."""
from __future__ import annotations

import argparse
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from vupai import audio, tmuxio
from vupai.asr import ParakeetTranscriber, model_cached
from vupai.commands import _CLOSE_VERBS, _CREATE_VERBS, wrap_agent_command
from vupai.config import (
    CONFIG_PATH,
    Config,
    load_config,
    set_mic_device,
    write_journal_config,
)
from vupai.daemon import Daemon
from vupai.feedback import Feedback
from vupai.permissions import (
    check_permissions,
    fixes,
    hints,
    missing_tools,
    open_settings_pane,
    terminal_app,
)
from vupai.recorder import Recorder
from vupai.registry import PaneRegistry
from vupai.router import name_collides, next_callsign
from vupai.tmuxio import TmuxError

PIDFILE: Path = Path.home() / ".config" / "vupai" / "daemon.pid"
DAEMON_LOG: Path = Path.home() / ".config" / "vupai" / "daemon.log"


def _daemon_running() -> bool:
    """True if a daemon pid is recorded and that process is still alive."""
    if not PIDFILE.exists():
        return False
    try:
        pid = int(PIDFILE.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, 0)  # signal 0: existence check, doesn't actually signal
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive but owned by someone else (shouldn't happen)
    return True


def _spawn_daemon() -> None:
    """Launch the daemon as a detached background process.

    CRITICAL: the daemon must NOT run inside a tmux window. A global pynput key
    listener only receives events if its macOS "responsible process" holds Input
    Monitoring + Accessibility. Inside tmux the responsible process is the long-
    lived tmux server (which lacks those grants), so the hotkey silently never
    fires. Spawned here, the daemon's responsible process is the terminal app
    that launched `vupai`, which the user already granted - so the hotkey works.
    """
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    # Truncate: each daemon start gets a fresh log so stale tracebacks from a
    # previous (possibly pre-fix) run can't pile up and mislead. The fd is
    # inherited by the child and our copy is released when this process exits.
    log = open(DAEMON_LOG, "w")  # noqa: SIM115 - handed to the child process
    proc = subprocess.Popen(
        [sys.executable, "-m", "vupai", "_daemon"],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach from our controlling terminal
    )
    # Record the pid now (the child also writes it) to avoid a double-spawn race.
    PIDFILE.write_text(str(proc.pid))


def _self_cmd() -> str:
    """How tmux hooks/bindings should re-invoke this CLI.

    Uses the absolute venv interpreter so `vupai` need not be on tmux's PATH
    (run-shell executes via /bin/sh, which lacks the venv activation).
    """
    return f"{sys.executable} -m vupai"


def _autoname_unnamed_panes() -> None:
    """One-time sweep: give every currently-unnamed pane a callsign.

    The pane-creation hooks only fire for panes created *after* they are
    installed, so the session's initial pane (created by `new-session`, which
    fires no split/new-window hook) and any pre-existing panes when attaching to
    a running server would otherwise stay nameless. Idempotent and silent.
    """
    try:
        registry = PaneRegistry()
        registry.refresh()
    except TmuxError:
        return
    used = [p.name for p in registry.panes if p.name != p.id]
    cutoff = load_config().fuzzy_cutoff
    for pane in registry.panes:
        if pane.name != pane.id:
            continue  # already named
        callsign = next_callsign(used, fuzzy_cutoff=cutoff)
        if callsign is None:
            break  # pool exhausted
        tmuxio.set_pane_name(pane.id, callsign)
        used.append(callsign)


def _initial_pane_command(cfg: Config) -> str:
    """Program for the session's first pane: the agent if it's on PATH, else a shell.

    vupai is agent-first - the initial pane runs `pane_command` (the agentic
    tool, e.g. "claude") so a fresh session opens with an agent ready to address.
    But `new-session` with a command that exits at once would kill the session
    (no windows remain), so an agent that isn't installed degrades to a plain
    shell. An empty `pane_command` is an intentional shell default, not an error.
    """
    prog = cfg.pane_command
    if prog and shutil.which(shlex.split(prog)[0]):
        return prog
    return ""  # plain shell


def ensure_up() -> None:
    """Start the tmux server, configure naming, and ensure the voice daemon is running."""
    cfg = load_config()
    if not tmuxio.server_running():
        # Start a detached server. NOTE: tmuxio.run() already prepends "tmux",
        # so the argv must NOT include it again. Open the initial pane on the
        # agent (agent-first); fall back to a shell if it isn't installed.
        argv = ["new-session", "-d", "-s", "vupai"]
        prog = _initial_pane_command(cfg)
        if prog:
            # Single arg: tmux runs it through the shell, so the wrapper's
            # `exec $SHELL` drops the pane to a terminal when the agent exits.
            argv.append(wrap_agent_command(prog))
        elif cfg.pane_command:
            print(f"'{cfg.pane_command}' not found on PATH - opening a shell "
                  "instead. Install it or set pane_command in the config.")
        tmuxio.run(argv)
    tmuxio.enable_pane_titles()
    tmuxio.set_extended_keys_off()
    if cfg.status_indicator:
        tmuxio.install_status_indicator()  # ambient daemon-state in status-right
    else:
        tmuxio.restore_status_right()      # opted out: hand status-right back
    self_cmd = _self_cmd()
    tmuxio.set_pane_autoname_hooks(self_cmd)  # new panes auto-get a callsign
    tmuxio.bind_rename_key(self_cmd)          # <prefix>+R renames the active pane
    _autoname_unnamed_panes()                 # name the initial pane the hooks miss
    if not _daemon_running():
        # First run downloads the speech model (~600MB) inside the detached
        # daemon, which blocks warm() *before* the hotkey listener starts - so
        # the hotkey looks dead until it finishes, with output buried in the
        # daemon log. Warn up front so a slow cold start isn't mistaken for a
        # broken hotkey. (Run `vupai setup` to download it visibly first.)
        if not model_cached(cfg.model_id):
            print("First run: the daemon is downloading the speech model "
                  "(~600MB, one time).")
            print("The hotkey won't respond until it finishes. Watch progress:")
            print(f"  tail -f {DAEMON_LOG}")
        _spawn_daemon()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_up(args: argparse.Namespace) -> int:
    ensure_up()
    return 0


def _cmd_default(args: argparse.Namespace) -> int:
    # `--reload` respawns the daemon first so source edits take effect, then
    # attaches - collapsing the `vupai reload && vupai` dogfooding loop.
    if getattr(args, "reload", False):
        _cmd_down(args)
    ensure_up()
    # `tmux attach` refuses to nest, so attaching from inside a pane would fail
    # (and looks like a broken reload). When already inside tmux, the daemon has
    # been respawned and there's nowhere new to attach - so just report and stop.
    if tmuxio.inside_tmux():
        print("Already inside tmux - daemon reloaded; staying in the current "
              "session (skipped attach to avoid nesting).")
        return 0
    tmuxio.attach()
    return 0


def _cmd_down(args: argparse.Namespace) -> int:
    # Terminate the daemon process if we recorded its pid. The daemon is a
    # detached background process (not a tmux window), so SIGTERM is all it takes.
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass
        PIDFILE.unlink(missing_ok=True)
    return 0


def _cmd_reload(args: argparse.Namespace) -> int:
    """Stop a running daemon, then start a fresh one so code changes take effect.

    The daemon loads vupai's modules once at spawn time, so edits to the source
    are invisible until it is respawned. `reload` is `down` + `ensure_up` in a
    single step for the edit-test loop while dogfooding vupai on itself.
    """
    _cmd_down(args)
    ensure_up()
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    registry = PaneRegistry()
    registry.refresh()
    print("panes:")
    for p in registry.panes:
        active = "*" if p.active else " "
        print(f"  {active} {p.id} [{p.window}/{p.index}] {p.name or '-'} ({p.command})")
    if _daemon_running():
        print(f"daemon: running (pid {PIDFILE.read_text().strip()})")
        print(f"  log: {DAEMON_LOG}  (tail -f to watch)")
    else:
        print("daemon: not running")
    cfg = load_config()
    if model_cached(cfg.model_id):
        print(f"speech model: ready ({cfg.model_id})")
    else:
        print(f"speech model: NOT downloaded ({cfg.model_id}) - "
              "first run will fetch ~600MB before the hotkey responds")
    status = check_permissions()
    print(
        f"permissions: microphone={status.microphone} "
        f"input_monitoring={status.input_monitoring} "
        f"accessibility={status.accessibility}"
    )
    return 0


def _cmd_name(args: argparse.Namespace) -> int:
    cfg = load_config()
    reserved = {cfg.broadcast_word.strip().lower()}
    if args.name.strip().lower() in reserved:
        print(f"name '{args.name}' is reserved (broadcast word)")
        return 1
    registry = PaneRegistry()
    registry.refresh()
    existing = [p.name for p in registry.panes if p.name]
    collision = name_collides(args.name, existing, fuzzy_cutoff=cfg.fuzzy_cutoff)
    if collision is not None:
        print(f"name '{args.name}' collides with existing pane '{collision}'")
        return 1
    target = args.pane or tmuxio.focused_pane_id()
    if target is None:
        print("no focused pane to name")
        return 1
    tmuxio.set_pane_name(target, args.name)
    return 0


def _cmd_autoname(args: argparse.Namespace) -> int:
    """Assign the next free callsign to a pane, unless it is already named.

    Invoked by the tmux after-split/after-new-window hooks for each new pane;
    also usable by hand. Idempotent: a pane that already has a name is left
    alone, so re-firing the hook never relabels an agent.
    """
    registry = PaneRegistry()
    registry.refresh()
    target = args.pane or tmuxio.focused_pane_id()
    if target is None:
        print("no pane to name")
        return 1
    pane = next((p for p in registry.panes if p.id == target), None)
    # name == id means unnamed; anything else is a real, user-or-auto name.
    if pane is not None and pane.name != pane.id:
        print(f"{target} already named '{pane.name}'")
        return 0
    used = [p.name for p in registry.panes if p.name != p.id]
    callsign = next_callsign(used, fuzzy_cutoff=load_config().fuzzy_cutoff)
    if callsign is None:
        print("no free callsign available")
        return 0
    tmuxio.set_pane_name(target, callsign)
    print(f"named {target} '{callsign}'")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    missing = missing_tools()
    for pkg in missing:
        print(f"{pkg}: not found on PATH - install it with `brew install {pkg}`")
    status = check_permissions()
    sox_missing = "sox" in missing
    hint_lines = hints(status)
    for line in hint_lines:
        # Without sox the mic probe can't even run, so its "grant Microphone"
        # hint is misleading - the real fix (install sox) is printed above.
        if sox_missing and line.startswith("Microphone"):
            continue
        print(line)
    # The model download is a separate first-run blocker from permissions: a
    # cold cache makes the hotkey unresponsive for minutes even with every grant
    # in place. Surface it so "All checks passed" can't hide it.
    cfg = load_config()
    model_ready = model_cached(cfg.model_id)
    if not model_ready:
        print(f"Speech model not downloaded ({cfg.model_id}); the first "
              "`vupai` launch fetches ~600MB before the hotkey responds. "
              "Run `vupai setup` to download it now.")
    if not missing and not hint_lines and model_ready:
        print("All checks passed.")
    return 0


def _ensure_model_ready(cfg: Config) -> None:
    """Make the speech-model download a visible, foreground step of `setup`.

    On first run the model is otherwise fetched silently inside the detached
    daemon (blocking the hotkey). Doing it here - in the foreground, with the
    HF progress bar on the user's terminal - turns the slow part into something
    they can watch instead of guessing whether the hotkey is broken. A failure
    is non-fatal: the daemon retries the download on first launch.
    """
    if model_cached(cfg.model_id):
        print(f"Speech model: ready ({cfg.model_id})")
        return
    print(f"Speech model: not downloaded yet - fetching {cfg.model_id}")
    print("  ~600MB, one time; this can take a few minutes (progress below).")
    try:
        ParakeetTranscriber(cfg.model_id).warm()
    except Exception as exc:  # network/cache errors must not abort setup
        print(f"Speech model: download did not complete ({exc}).")
        print("  It will be retried automatically on first `vupai` launch.")
        return
    print("Speech model: downloaded and ready.")


def _prompt_yes_no(question: str, *, default: bool, reader=input) -> bool:
    """Ask a yes/no question; a bare Enter (or non-tty EOF) keeps `default`."""
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = reader(question + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _prompt_journal_setup(*, reader=None, config_path: Path | None = None) -> None:
    """First-run only: ask whether to journal and to retain audio, then write a
    starter config. Skipped silently once a config file exists, so re-running
    `setup` to confirm permissions never re-prompts."""
    reader = reader if reader is not None else input
    config_path = config_path if config_path is not None else CONFIG_PATH
    if config_path.exists():
        return
    print("\nUtterance journal (transcript + decision per utterance, for "
          "diagnosing misfires):")
    enabled = _prompt_yes_no("  Keep a journal?", default=True, reader=reader)
    keep_audio = False
    if enabled:
        keep_audio = _prompt_yes_no(
            "  Also retain audio recordings (your voice) for offline replay?",
            default=False, reader=reader)
    write_journal_config(
        enabled=enabled, keep_audio=keep_audio, path=config_path)
    print(f"  Wrote {config_path} "
          f"(journal_enabled={enabled}, journal_keep_audio={keep_audio}).")


def _format_device_line(index, device, *, selected: str, prefix: bool) -> str:
    """Render one input-device row for `vupai mic` / the setup prompt."""
    marks = []
    if device.is_default:
        marks.append("default")
    is_selected = bool(selected) and device.name == selected
    if is_selected:
        marks.append("selected")
    suffix = f"   ({', '.join(marks)})" if marks else ""
    mark = ("*" if is_selected else " ") + " " if prefix else ""
    return f"{mark}{index}  {device.name}{suffix}"


def _resolve_mic_selection(selection: str, devices) -> tuple[str | None, str | None]:
    """Map a user token (index, name, or 'default') to the name to store.

    Returns ``(name_to_store, error)``. ``'default'`` -> ``("", None)`` clears
    the pin. On a bad index/name, ``name`` is None and ``error`` is set.
    """
    if selection == "default":
        return "", None
    if selection.isdigit():
        idx = int(selection)
        if 0 <= idx < len(devices):
            return devices[idx].name, None
        upper = len(devices) - 1
        return None, f"No device at index {idx} (have 0..{upper})."
    for device in devices:
        if device.name.lower() == selection.lower():
            return device.name, None
    return None, f"No input device matches {selection!r}."


def _cmd_mic(args: argparse.Namespace) -> int:
    """List input devices, or pin one for the recorder.

    No argument lists devices (marking the system default and current pin). An
    index, exact name, or the literal `default` (to unpin) persists the choice;
    a running daemon must `vupai reload` to pick it up.
    """
    devices = audio.list_input_devices()
    cfg = load_config()
    if not devices:
        print("No input devices found (is `system_profiler` available?).")
        return 1

    if args.selection is None:
        for i, device in enumerate(devices):
            print(_format_device_line(
                i, device, selected=cfg.mic_device, prefix=True))
        if cfg.mic_device:
            print(f"\nPinned: {cfg.mic_device}. "
                  "`vupai mic default` to use the system default.")
        else:
            print("\nUsing the system default. "
                  "`vupai mic <index|name>` to pin one.")
        return 0

    name, error = _resolve_mic_selection(args.selection, devices)
    if error:
        print(error)
        return 1
    # Probe a specific device before pinning so an unusable one (e.g. a name that
    # collides with an output) fails loudly here instead of silently yielding
    # "no audio captured" at speech time. Clearing the pin ("default") needs no
    # probe. `--force` pins anyway.
    if name and not getattr(args, "force", False):
        probe_error = audio.probe_capture(name)
        if probe_error:
            print(f"Cannot use {name!r}: {probe_error}")
            print("Pick another device, or `vupai mic <name> --force` to pin "
                  "it anyway.")
            return 1
    set_mic_device(name)
    label = name if name else "system default"
    print(f"Mic set to: {label}")
    if _daemon_running():
        print("Run `vupai reload` for the daemon to pick it up.")
    return 0


def _prompt_mic_setup(*, reader=None, runner=None, config_path: Path | None = None) -> None:
    """Setup step: list input devices and let the user pin one.

    Re-runnable (unlike the first-run journal prompt): shows the current pin and
    a bare Enter keeps it. Silently no-ops when no devices are enumerable."""
    reader = reader if reader is not None else input
    devices = audio.list_input_devices(runner=runner)
    if not devices:
        return
    cfg = load_config(config_path)
    print("\nMicrophone (input device for speech):")
    for i, device in enumerate(devices):
        print("  " + _format_device_line(
            i, device, selected=cfg.mic_device, prefix=False))
    current = cfg.mic_device or "system default"
    try:
        answer = reader(f"  Choice [keep {current}]: ").strip()
    except EOFError:
        return
    if not answer:
        return
    name, error = _resolve_mic_selection(answer, devices)
    if error:
        print(f"  {error} Keeping {current}.")
        return
    if name:
        probe_error = audio.probe_capture(name)
        if probe_error:
            print(f"  Cannot use {name!r}: {probe_error}. Keeping {current}.")
            return
    set_mic_device(name, path=config_path)
    print(f"  Mic set to: {name if name else 'system default'}.")


def _cmd_setup(args: argparse.Namespace) -> int:
    """Interactive permission bootstrap: probe, then deep-link the user to each
    failing pane (naming the exact terminal app to enable). Cannot grant on the
    user's behalf - macOS TCC requires a human click - but removes all the
    navigation and ambiguity.
    """
    missing = missing_tools()
    for pkg in missing:
        print(f"{pkg}: not found on PATH - install it with `brew install {pkg}`")
    if missing:
        print("Install the tool(s) above, then re-run `vupai setup`.")
        return 1

    # First-run only: capture journaling consent before anything is recorded.
    _prompt_journal_setup()

    # Re-runnable: pick the input device (bare Enter keeps the current choice).
    _prompt_mic_setup()

    # Download the speech model up front (visible) so the daemon's first run
    # doesn't stall the hotkey on a silent multi-minute fetch.
    _ensure_model_ready(load_config())

    app = terminal_app()
    label = app.name + (f" ({app.bundle_id})" if app.bundle_id else "")
    print(f"Terminal app: {label}")
    print("Probing permissions (approve any macOS prompt that appears)...")
    status = check_permissions()
    pending = fixes(status)
    if not pending:
        print("All permissions granted. You're ready - run `vupai`.")
        return 0

    print(f"\n{len(pending)} permission(s) still needed - "
          f"enable {app.name} in each pane that opens:")
    for fix in pending:
        print(f"\n  {fix.label}: toggle {app.name} ON in the opened pane")
        if app.bundle_id:
            print(f"    if {app.name} is missing or stuck off, reset and retry:")
            print(f"      tccutil reset {fix.reset_service} {app.bundle_id}")
        open_settings_pane(fix.url)
    print("\nAfter enabling them, re-run `vupai setup` to confirm.")
    return 1


def _voice_commands_text(cfg: Config) -> str:
    """Render a quick reference of the spoken commands for the active config.

    Config-driven so the broadcast word, hotkeys, program tokens and macros
    shown match the user's setup; verb sets come from commands.py so they never
    drift from the parser.
    """
    create_verbs = " / ".join((*_CREATE_VERBS, "spin up"))
    close_alts = " / ".join(_CLOSE_VERBS[1:])  # row label is the first verb already
    programs = " / ".join(sorted(cfg.programs)) or "(none)"
    slash_verbs = " / ".join(sorted(cfg.slash_commands)) or "(none)"
    lines = ["vupai voice commands", ""]

    if cfg.addressing != "button":
        # Keyword mode is a single key with no command layer: dictation, name
        # addressing, and broadcast only. Commands live on the button system key.
        lines += [
            f"Addressing mode: keyword (hold {cfg.hotkey}, then speak)",
            "  no command layer here - switch to button mode for commands",
            "",
            f"Broadcast: {cfg.broadcast_word} <message>   send <message> to every named agent",
            "",
            "Address an agent (no prefix):",
            '  <name>, <message>              e.g. "nova, run the tests" -> the nova pane',
            "",
            "Anything else is typed verbatim into the focused pane.",
        ]
        return "\n".join(lines)

    lines += [
        "Addressing mode: button (hold a key, then speak)",
        f"  system key    ({cfg.command_hotkey}): a command, broadcast, or an agent by name",
        f"  dictation key ({cfg.hotkey}): typed verbatim into the focused pane",
        "",
        "Commands (hold the system key, then speak):",
        "  create <n> panes [program]   spin up n auto-named panes, tiled",
        f"      verbs: {create_verbs}   n: 1-9 (or one..nine)   program: {programs}",
        "  focus <name>                 focus a pane (also: switch to / go to <name>)",
        "  swap <name> and <name>       swap two named panes",
        f"  close <name>                 close a pane (also: {close_alts} <name>)",
        "  close the others             close every pane but the focused one",
        "  zoom [name]                  zoom a pane (also: maximize / full screen)",
        "  unzoom                       restore layout (also: minimize / restore)",
        "  <slash> [name|all]           send a slash command (focused / named / all)",
        f'      slash: {slash_verbs}   e.g. "clear all" -> /clear to every agent',
        "",
        f"Broadcast: {cfg.broadcast_word} <message>   send <message> to every named agent",
        "",
        "Address an agent (hold the system key):",
        '  <name>, <message>              e.g. "nova, run the tests" -> the nova pane',
        "",
        "Macros:",
    ]
    if cfg.macros:
        for phrase, actions in cfg.macros.items():
            lines.append(f"  {phrase}  ->  {', '.join(actions)}")
    else:
        lines.append("  (none configured)")

    return "\n".join(lines)


def _cmd_voice_commands(args: argparse.Namespace) -> int:
    print(_voice_commands_text(load_config()))
    return 0


def _cmd_daemon(args: argparse.Namespace) -> int:
    # Route our loggers (asr model id/warnings, daemon errors) to the inherited
    # stdout fd so they land in daemon.log; without this nothing below WARNING
    # from our modules is emitted and "loading parakeet model X" is invisible.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))
    cfg = load_config()
    device, warning = audio.resolve_device(cfg.mic_device)
    if warning:
        logging.getLogger("vupai.recorder").warning(warning)
    recorder = Recorder(sample_rate=cfg.sample_rate, device=device)
    transcriber = ParakeetTranscriber(cfg.model_id)
    registry = PaneRegistry()
    feedback = Feedback(indicator_enabled=cfg.status_indicator)
    Daemon(cfg, recorder, transcriber, registry, feedback).run()
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vupai")
    parser.set_defaults(func=_cmd_default)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="respawn the daemon (pick up source edits) before attaching",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("up").set_defaults(func=_cmd_up)
    sub.add_parser("down").set_defaults(func=_cmd_down)
    sub.add_parser(
        "reload", help="restart the daemon so source edits take effect"
    ).set_defaults(func=_cmd_reload)
    sub.add_parser("status").set_defaults(func=_cmd_status)

    p_name = sub.add_parser("name")
    p_name.add_argument("name")
    p_name.add_argument("pane", nargs="?", default=None)
    p_name.set_defaults(func=_cmd_name)

    p_autoname = sub.add_parser("autoname")
    p_autoname.add_argument("pane", nargs="?", default=None)
    p_autoname.set_defaults(func=_cmd_autoname)

    p_mic = sub.add_parser(
        "mic", help="list input devices, or pin one (index|name|default)")
    p_mic.add_argument("selection", nargs="?", default=None)
    p_mic.add_argument(
        "--force", action="store_true",
        help="pin even if the capture probe fails")
    p_mic.set_defaults(func=_cmd_mic)

    sub.add_parser("doctor").set_defaults(func=_cmd_doctor)
    sub.add_parser(
        "setup", help="grant macOS permissions interactively (opens Settings panes)"
    ).set_defaults(func=_cmd_setup)
    sub.add_parser(
        "voice-commands", help="print the spoken-command cheat sheet"
    ).set_defaults(func=_cmd_voice_commands)

    # Hidden: internal entrypoint the voice window runs; not shown in --help.
    # Registered directly in the name map rather than via add_parser so it
    # never appears in format_help() output.
    hidden = argparse.ArgumentParser(prog="vupai _daemon")
    hidden.set_defaults(func=_cmd_daemon, command="_daemon")
    sub._name_parser_map["_daemon"] = hidden

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
