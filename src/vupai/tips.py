"""Rotating status-bar tips: ambient discoverability for the voice grammar.

vupai has no on-screen menu (it is voice-driven), so users forget the available
commands - especially their own macros and slash verbs. `build_tips` renders a
config-generated pool of short example sentences; `TipRotator` cycles them into
the tmux status-left segment (see tmuxio.install_tip_segment).

MAINTENANCE: when a new spoken command, slash verb, macro, or program is added,
consider adding a matching example here and confirm with the user whether it
belongs in the rotation (see AGENTS.md). The verb examples reuse the parser's
verb constants so they cannot drift from commands.py.
"""
from __future__ import annotations

import logging
import threading

from vupai import tmuxio
from vupai.commands import _CLOSE_VERBS, _CREATE_VERBS

logger = logging.getLogger(__name__)

_TIP_PREFIX = "tip: "
_TIP_MAX = 48  # truncate so the status-left segment stays compact


def _render(text: str) -> str:
    return (_TIP_PREFIX + text)[:_TIP_MAX]


def _interleave(a: list[str], b: list[str]) -> list[str]:
    """Weave two lists so consecutive items differ in kind; trailing remainder
    of the longer list is appended in order. Deterministic (no RNG)."""
    out: list[str] = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
    return out


def build_tips(cfg) -> list[str]:
    """Build the rotating tip pool for `cfg`. Deterministic order, never empty.

    Button mode shows command examples (static verbs + the user's slash verbs,
    macros, and programs); keyword mode has no command layer, so only the
    broadcast and usage hints appear.
    """
    talk_key = cfg.command_hotkey if cfg.addressing == "button" else cfg.hotkey
    hints = [
        f"hold {talk_key} to talk",
        "set status_tips=false in config.toml to hide tips",
    ]
    commands: list[str] = []
    if cfg.addressing == "button":
        commands += [
            f"{_CREATE_VERBS[0]} two panes",
            "focus nova",
            "zoom nova",
            f"{_CLOSE_VERBS[0]} nova",
            "swap nova and atlas",
        ]
        commands += [f"{verb} all" for verb in sorted(cfg.slash_commands)]
        commands += list(cfg.macros)
        if cfg.programs:
            commands.append(f"create one {sorted(cfg.programs)[0]} pane")
    # Broadcast exists in both addressing modes.
    commands.append(f"{cfg.broadcast_word} ship it")
    return [_render(t) for t in _interleave(commands, hints)]
