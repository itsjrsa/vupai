"""voxpane CLI entry point — subcommand dispatch."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from voxpane import tmuxio
from voxpane.asr import ParakeetTranscriber
from voxpane.commands import _CLOSE_VERBS, _CREATE_VERBS
from voxpane.config import Config, load_config
from voxpane.daemon import Daemon
from voxpane.feedback import Feedback
from voxpane.permissions import check_permissions, hints, missing_tools
from voxpane.recorder import Recorder
from voxpane.registry import PaneRegistry
from voxpane.router import name_collides, next_callsign
from voxpane.tmuxio import TmuxError

PIDFILE: Path = Path.home() / ".config" / "voxpane" / "daemon.pid"
DAEMON_LOG: Path = Path.home() / ".config" / "voxpane" / "daemon.log"


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
    that launched `voxpane`, which the user already granted - so the hotkey works.
    """
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    # Truncate: each daemon start gets a fresh log so stale tracebacks from a
    # previous (possibly pre-fix) run can't pile up and mislead. The fd is
    # inherited by the child and our copy is released when this process exits.
    log = open(DAEMON_LOG, "w")  # noqa: SIM115 - handed to the child process
    proc = subprocess.Popen(
        [sys.executable, "-m", "voxpane", "_daemon"],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach from our controlling terminal
    )
    # Record the pid now (the child also writes it) to avoid a double-spawn race.
    PIDFILE.write_text(str(proc.pid))


def _self_cmd() -> str:
    """How tmux hooks/bindings should re-invoke this CLI.

    Uses the absolute venv interpreter so `voxpane` need not be on tmux's PATH
    (run-shell executes via /bin/sh, which lacks the venv activation).
    """
    return f"{sys.executable} -m voxpane"


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


def ensure_up() -> None:
    """Start the tmux server, configure naming, and ensure the voice daemon is running."""
    if not tmuxio.server_running():
        # Start a detached server. NOTE: tmuxio.run() already prepends "tmux",
        # so the argv must NOT include it again.
        tmuxio.run(["new-session", "-d", "-s", "voxpane"])
    tmuxio.enable_pane_titles()
    tmuxio.set_extended_keys_off()
    self_cmd = _self_cmd()
    tmuxio.set_pane_autoname_hooks(self_cmd)  # new panes auto-get a callsign
    tmuxio.bind_rename_key(self_cmd)          # <prefix>+R renames the active pane
    _autoname_unnamed_panes()                 # name the initial pane the hooks miss
    if not _daemon_running():
        _spawn_daemon()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_up(args: argparse.Namespace) -> int:
    ensure_up()
    return 0


def _cmd_default(args: argparse.Namespace) -> int:
    ensure_up()
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

    The daemon loads voxpane's modules once at spawn time, so edits to the source
    are invisible until it is respawned. `reload` is `down` + `ensure_up` in a
    single step for the edit-test loop while dogfooding voxpane on itself.
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
    status = check_permissions()
    print(
        f"permissions: microphone={status.microphone} "
        f"input_monitoring={status.input_monitoring} "
        f"accessibility={status.accessibility}"
    )
    return 0


def _cmd_name(args: argparse.Namespace) -> int:
    cfg = load_config()
    reserved = {cfg.control_word.strip().lower(), cfg.broadcast_word.strip().lower()}
    if args.name.strip().lower() in reserved:
        print(f"name '{args.name}' is reserved (control/broadcast word)")
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
    if not missing and not hint_lines:
        print("All checks passed.")
    return 0


def _voice_commands_text(cfg: Config) -> str:
    """Render a quick reference of the spoken commands for the active config.

    Config-driven so the control/broadcast words, hotkeys, program tokens and
    macros shown match the user's setup; verb sets come from commands.py so they
    never drift from the parser.
    """
    create_verbs = " / ".join((*_CREATE_VERBS, "spin up"))
    close_alts = " / ".join(_CLOSE_VERBS[1:])  # row label is the first verb already
    programs = " / ".join(sorted(cfg.programs)) or "(none)"
    slash_verbs = " / ".join(sorted(cfg.slash_commands)) or "(none)"
    lines = ["voxpane voice commands", ""]

    if cfg.addressing == "button":
        lines += [
            "Addressing mode: button (hold a key, then speak)",
            f"  system key    ({cfg.command_hotkey}): a command, broadcast, or an agent by name",
            f"  dictation key ({cfg.hotkey}): typed verbatim into the focused pane",
            "",
            "Commands (hold the system key, then speak):",
        ]
        prefix = ""
        name_intro = "Address an agent (hold the system key):"
    else:
        lines += [
            f"Addressing mode: keyword (hold {cfg.hotkey}, then speak)",
            "",
            f'Commands (prefix with "{cfg.control_word}"):',
        ]
        prefix = f"{cfg.control_word} "
        name_intro = "Address an agent (no prefix):"

    lines += [
        f"  {prefix}create <n> panes [program]   spin up n auto-named panes, tiled",
        f"      verbs: {create_verbs}   n: 1-9 (or one..nine)   program: {programs}",
        f"  {prefix}focus <name>                 focus a pane (also: switch to / go to <name>)",
        f"  {prefix}swap <name> and <name>       swap two named panes",
        f"  {prefix}close <name>                 close a pane (also: {close_alts} <name>)",
        f"  {prefix}close the others             close every pane but the focused one",
        f"  {prefix}zoom [name]                  zoom a pane (also: maximize / full screen)",
        f"  {prefix}unzoom                       restore layout (also: minimize / restore)",
        f"  {prefix}<slash> [name|all]           send a slash command (focused / named / all)",
        f"      slash: {slash_verbs}   e.g. \"{prefix}clear all\" -> /clear to every agent",
        "",
        f"Broadcast: {cfg.broadcast_word} <message>   send <message> to every named agent",
        "",
        name_intro,
        '  <name>, <message>              e.g. "nova, run the tests" -> the nova pane',
        "",
        "Macros:",
    ]
    if cfg.macros:
        for phrase, actions in cfg.macros.items():
            lines.append(f"  {prefix}{phrase}  ->  {', '.join(actions)}")
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
    recorder = Recorder(sample_rate=cfg.sample_rate)
    transcriber = ParakeetTranscriber(cfg.model_id)
    registry = PaneRegistry()
    feedback = Feedback()
    Daemon(cfg, recorder, transcriber, registry, feedback).run()
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voxpane")
    parser.set_defaults(func=_cmd_default)
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

    sub.add_parser("doctor").set_defaults(func=_cmd_doctor)
    sub.add_parser(
        "voice-commands", help="print the spoken-command cheat sheet"
    ).set_defaults(func=_cmd_voice_commands)

    # Hidden: internal entrypoint the voice window runs; not shown in --help.
    # Registered directly in the name map rather than via add_parser so it
    # never appears in format_help() output.
    hidden = argparse.ArgumentParser(prog="voxpane _daemon")
    hidden.set_defaults(func=_cmd_daemon, command="_daemon")
    sub._name_parser_map["_daemon"] = hidden

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
