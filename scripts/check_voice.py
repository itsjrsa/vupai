#!/usr/bin/env python3
"""Typed tester / debugger for vupai voice actions: no mic, no ASR, no daemon.

Type what you'd SAY (or pass it on the CLI) and it runs through the SAME pipeline
the daemon uses for the chosen keybind, against the focused pane:

  - dictation key: the text is typed verbatim into the focused pane.
  - system key:    a command (read / create / focus / clear all / ...), or an
                   utterance addressed to a pane by name; unaddressed text is
                   rejected, exactly as the real system key does.

It drives the real parse/route/inject functions, so behavior matches production.
Injection and commands really fire (destructive kinds prompt first); `read` speaks
aloud like the real command. Pass --dry to see decisions with no side effects, or
--silent to keep `read` quiet. Config overrides (--summarizer / --tts-cmd / ...)
and --pane (force the focused pane) make it a deterministic debugging harness.

    uv run python scripts/check_voice.py                      # interactive, speaks aloud
    uv run python scripts/check_voice.py -m system read nova  # one-shot (no quotes needed)
    uv run python scripts/check_voice.py --pane atlas --dry -m system close nova
    uv run python scripts/check_voice.py --summarizer 'echo hi' -m system read nova
    uv run python scripts/check_voice.py --list              # just list panes and exit
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from vupai.commands import DESTRUCTIVE_KINDS, execute_command, parse_command
from vupai.config import load_config
from vupai.injector import inject
from vupai.registry import PaneRegistry
from vupai.router import resolve_pane_by_name, route

_QUIT = {"q", "quit", "exit"}
_MODES = ("system", "dictation")


def _make_config(args):
    """Real config (or --config file), with the CLI's field overrides applied."""
    cfg = load_config(Path(args.config)) if args.config else load_config()
    overrides = {}
    if args.silent:
        overrides["tts_enabled"] = False
    if args.tts_cmd is not None:
        overrides["tts_cmd"] = args.tts_cmd
    if args.summarizer is not None:
        overrides["board_summarizer_cmd"] = args.summarizer
    if args.fuzzy_cutoff is not None:
        overrides["fuzzy_cutoff"] = args.fuzzy_cutoff
    return replace(cfg, **overrides) if overrides else cfg


def _show_panes(reg):
    focused = reg.focused()
    fid = focused.id if focused is not None else None
    print("Panes:")
    for p in reg.panes:
        name = p.name if p.name != p.id else "(unnamed)"
        flags = [f for f, on in (("active", p.active), ("focused", p.id == fid)) if on]
        tag = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {name:<12} {p.id:<6} {p.session}{tag}")


def _resolve_focus(reg, spec, cfg):
    """Map a --pane spec (a %id or a voice name) to a pane id, or None."""
    if any(p.id == spec for p in reg.panes):
        return spec
    return resolve_pane_by_name(spec, reg.panes, fuzzy_cutoff=cfg.fuzzy_cutoff).pane_id


def _focus_id(reg, cfg, forced):
    """The pane to treat as focused: the --pane override, else tmux's actual focus."""
    if forced:
        return _resolve_focus(reg, forced, cfg)
    f = reg.focused()
    return f.id if f is not None else None


def _inject(pane_id, text, cfg):
    return inject(pane_id, text, confirm_timeout=cfg.inject_confirm_timeout,
                  poll_interval=cfg.inject_poll_interval)


def _process(text, mode, reg, cfg, *, dry, forced=None):
    """Run one utterance under the chosen keybind, like daemon._process."""
    fid = _focus_id(reg, cfg, forced)
    if forced and fid is None:
        print(f"  no pane matching {forced!r}")
        return

    if mode == "dictation":
        if fid is None:
            print("  no focused pane")
            return
        print(f"  dictation -> {fid}: {text!r}")
        if not dry:
            print(f"  -> injected={_inject(fid, text, cfg)}")
        return

    # System key (button): a command, or an utterance addressed to a pane by name.
    cmd = parse_command(text, broadcast_word=cfg.broadcast_word, macros=cfg.macros,
                        programs=cfg.programs, slash_commands=cfg.slash_commands,
                        addressing="button")
    if cmd is not None:
        print(f"  command: {cmd.kind}" + (f" name={cmd.name!r}" if cmd.name else ""))
        if dry:
            print("  (dry-run)")
            return
        if cmd.kind in DESTRUCTIVE_KINDS:
            if input(f"  {cmd.kind} is destructive - really? [y/N] ").strip().lower() != "y":
                print("  cancelled")
                return
        res = execute_command(cmd, reg, cfg)
        print(f"  -> ok={res.ok}: {res.message}")
        return

    r = route(text, reg.panes, fid, fuzzy_cutoff=cfg.fuzzy_cutoff)
    if r.candidates:
        print("  ambiguous: " + " / ".join(r.candidates))
    elif r.fallback:
        # Faithful to the system key: unaddressed text is the dictation key's job.
        print("  rejected: not a command - name a pane, or use the dictation key")
    elif r.pane_id is None:
        print("  no target")
    else:
        print(f"  route -> {r.matched_name} ({r.pane_id}): {r.text!r}")
        if not dry:
            print(f"  -> injected={_inject(r.pane_id, r.text, cfg)}")


def _pick_mode():
    raw = input("Keybind? [s]ystem / [d]ictation: ").strip().lower()
    if raw in _QUIT:
        return None
    if raw in ("s", "system"):
        return "system"
    if raw in ("d", "dictation"):
        return "dictation"
    return _pick_mode()


def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog="check_voice.py", description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("text", nargs="*", help="utterance to run once; omit for an interactive loop")
    p.add_argument("-m", "--mode", choices=_MODES,
                   help="keybind to simulate (default: ask, or 'system' in one-shot)")
    p.add_argument("-p", "--pane", metavar="NAME|%ID",
                   help="treat this pane as focused, instead of tmux's actual focus")
    p.add_argument("--dry", action="store_true",
                   help="show decisions only; never execute or inject")
    p.add_argument("--silent", "--quiet", dest="silent", action="store_true",
                   help="force TTS off (read prints its summary without speaking)")
    p.add_argument("--tts-cmd", metavar="CMD", help="override tts_cmd (the speak command)")
    p.add_argument("--summarizer", metavar="CMD",
                   help="override board_summarizer_cmd (what read summarizes with)")
    p.add_argument("--fuzzy-cutoff", type=int, metavar="N",
                   help="override fuzzy_cutoff for name matching")
    p.add_argument("--config", metavar="PATH", help="load this config file instead of the default")
    p.add_argument("--list", action="store_true", help="list panes and exit")
    return p.parse_args(argv)


def main(argv):
    args = _parse_args(argv)
    cfg = _make_config(args)
    reg = PaneRegistry()
    reg.refresh()
    if not reg.panes:
        print("no panes found - is a tmux server running?")
        return 1

    _show_panes(reg)
    if args.list:
        return 0

    text = " ".join(args.text)
    if text:  # one-shot
        _process(text, args.mode or "system", reg, cfg, dry=args.dry, forced=args.pane)
        return 0

    mode = args.mode or _pick_mode()
    if mode is None:
        return 0

    bits = [f"{mode} key", "silent" if (args.silent or args.dry) else "speaks aloud"]
    if args.pane:
        bits.append(f"focus={args.pane}")
    print(f"[{' · '.join(bits)}]{' [dry]' if args.dry else ''}  type what you'd say; 'q' to quit.")
    while True:
        text = input(f"{mode}> ").strip()
        if text.lower() in _QUIT:
            return 0
        reg.refresh()
        if not text:
            _show_panes(reg)  # bare Enter re-lists panes
            continue
        _process(text, mode, reg, cfg, dry=args.dry, forced=args.pane)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (KeyboardInterrupt, EOFError):
        print()
