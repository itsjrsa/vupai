"""voxpane CLI entry point — subcommand dispatch."""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from voxpane import tmuxio
from voxpane.asr import ParakeetTranscriber
from voxpane.config import load_config
from voxpane.daemon import Daemon
from voxpane.feedback import Feedback
from voxpane.permissions import check_permissions, hints, missing_tools
from voxpane.recorder import Recorder
from voxpane.registry import PaneRegistry
from voxpane.router import name_collides
from voxpane.tmuxio import TmuxError

PIDFILE: Path = Path.home() / ".config" / "voxpane" / "daemon.pid"
DAEMON_CMD = f"{sys.executable} -m voxpane _daemon"


def ensure_up() -> None:
    """Start the tmux server, enable pane titles, and ensure the voice daemon window exists."""
    cfg = load_config()
    if not tmuxio.server_running():
        # Start a detached server. NOTE: tmuxio.run() already prepends "tmux",
        # so the argv must NOT include it again.
        tmuxio.run(["new-session", "-d", "-s", "voxpane"])
    tmuxio.enable_pane_titles()
    tmuxio.set_extended_keys_off()
    if not tmuxio.window_exists(cfg.voice_window_name):
        tmuxio.new_window(cfg.voice_window_name, DAEMON_CMD)


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
    # Terminate the daemon process if we recorded its pid.
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass
        PIDFILE.unlink(missing_ok=True)
    # Always tear down the voice window so a later `up` can recreate the daemon,
    # even when the pidfile is missing/stale (orphaned window).
    try:
        tmuxio.kill_window(load_config().voice_window_name)
    except TmuxError:
        pass
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    registry = PaneRegistry()
    registry.refresh()
    print("panes:")
    for p in registry.panes:
        active = "*" if p.active else " "
        print(f"  {active} {p.id} [{p.window}/{p.index}] {p.name or '-'} ({p.command})")
    if PIDFILE.exists():
        print(f"daemon: running (pid {PIDFILE.read_text().strip()})")
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
    tmuxio.set_pane_title(target, args.name)
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


def _cmd_daemon(args: argparse.Namespace) -> int:
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
    sub.add_parser("status").set_defaults(func=_cmd_status)

    p_name = sub.add_parser("name")
    p_name.add_argument("name")
    p_name.add_argument("pane", nargs="?", default=None)
    p_name.set_defaults(func=_cmd_name)

    sub.add_parser("doctor").set_defaults(func=_cmd_doctor)

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
