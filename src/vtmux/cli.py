"""vtmux CLI entry point — subcommand dispatch."""
from __future__ import annotations

import argparse
import os
import signal
from pathlib import Path

from vtmux import tmuxio
from vtmux.asr import ParakeetTranscriber
from vtmux.config import load_config
from vtmux.daemon import Daemon
from vtmux.feedback import Feedback
from vtmux.permissions import check_permissions, hints
from vtmux.recorder import Recorder
from vtmux.registry import PaneRegistry
from vtmux.router import name_collides

PIDFILE: Path = Path.home() / ".config" / "vtmux" / "daemon.pid"
DAEMON_CMD = "python -m vtmux _daemon"


def ensure_up() -> None:
    """Start the tmux server, enable pane titles, and ensure the voice daemon window exists."""
    cfg = load_config()
    if not tmuxio.server_running():
        # Start a detached server. NOTE: tmuxio.run() already prepends "tmux",
        # so the argv must NOT include it again.
        tmuxio.run(["new-session", "-d", "-s", "vtmux"])
    tmuxio.enable_pane_titles()
    tmuxio.set_extended_keys_off()
    if not tmuxio.window_exists(cfg.voice_window_name):
        tmuxio.new_window(cfg.voice_window_name, DAEMON_CMD)
        PIDFILE.parent.mkdir(parents=True, exist_ok=True)
        PIDFILE.write_text(str(os.getpid()))


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
    if not PIDFILE.exists():
        return 0
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError):
        pass
    PIDFILE.unlink(missing_ok=True)
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
    status = check_permissions()
    for line in hints(status):
        print(line)
    return 0


def _cmd_daemon(args: argparse.Namespace) -> int:
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
    parser = argparse.ArgumentParser(prog="vtmux")
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
    hidden = argparse.ArgumentParser(prog="vtmux _daemon")
    hidden.set_defaults(func=_cmd_daemon, command="_daemon")
    sub._name_parser_map["_daemon"] = hidden

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
