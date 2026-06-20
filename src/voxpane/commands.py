"""Command layer: interpret control-word utterances into structured Commands.

Interpretation (parse_command) is deliberately separate from execution
(execute_command, Task 6). The Command dataclass is the stable contract and the
seam for a future local-LLM interpreter: it would be another producer of
Commands, escalated to only on kind == "unknown". No LLM here.
"""
from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass

from voxpane import tmuxio
from voxpane.injector import inject
from voxpane.router import (
    _peel_fillers,
    next_callsign,
    resolve_pane_by_name,
    word_to_int,
)
from voxpane.tmuxio import TmuxError

_STRIP = ".,!?;:'\"()[]{}"
_CREATE_VERBS = ("create", "make", "add", "open", "new")
_CLOSE_VERBS = ("close", "kill")
_ZOOM_VERBS = ("zoom", "maximize")
_UNZOOM_VERBS = ("unzoom", "minimize", "restore")
# Parakeet splits "unzoom" into two tokens ("and zoom" / "un zoom"). Curated,
# deterministic - the leading token is implausible as a literal command on its
# own, so matching it here can't shadow a real utterance.
_UNZOOM_PHRASES = (["and", "zoom"], ["un", "zoom"])
# Unit nouns for `create`. "pane" is canonical; "agent"/"split" are homophone-free
# synonyms ("pane" mishears as "pain"/"panel") that map to the same thing - say
# whichever is natural. "window" stays distinct (real tmux concept, reserved for
# future window creation; _exec_create rejects it for now).
_UNITS = {
    "pane": "pane", "panes": "pane",
    "agent": "pane", "agents": "pane",
    "split": "pane", "splits": "pane",
    "window": "window", "windows": "window",
}
# Tokens that mean "one" before the unit ("create a pane" / "create another
# pane" == "create one pane"). Scoped to the create parse only - never fed to the
# global word_to_int, so these can't leak into router number-routing or dictation.
_ONE_WORDS = ("a", "an", "another")
# Descriptive filler that may sit between the count and the unit/program in a
# create utterance ("create a new pane", "create two new shell panes"). "new" is
# already a create verb, so as a mid-token it carries no extra meaning - drop it
# rather than failing the parse to `unknown`. Scoped to the create body only.
_CREATE_FILLERS = ("new", "fresh", "quick")
# Curated ASR mishearings of the unit noun -> canonical unit. The trailing unit
# token is the most-misheard part of "create N panes" ("paints"/"pains"). A
# fuzzy or phonetic match cannot help here: by Levenshtein ratio the real errors
# are inseparable from real words (pains==plans==lanes==80 vs "panes";
# paint==plain==66.7), so any threshold that catches the bug also turns
# "create three lanes" into panes. This table is deterministic instead: it lists
# only implausible-as-literal homophones and deliberately OMITS real words
# (panel/plane/plain/lane/plan), which stay non-units. A miss is safe (the
# utterance just becomes `unknown`, never injected); extend with a one-line edit
# plus a test when a new mishearing shows up in the wild.
_UNIT_ALIASES = {
    "pain": "pane", "pains": "pane", "paine": "pane", "payne": "pane",
    "paint": "pane", "paints": "pane", "pen": "pane", "pens": "pane",
    "windo": "window", "windos": "window", "windoes": "window",
}


def _resolve_unit(token: str) -> str | None:
    """Map a trailing token to a canonical unit ("pane"/"window"), or None.

    Exact unit words win; curated homophones are the deterministic fallback.
    `token` is already lowercased and punctuation-stripped by `_tokens`.
    """
    if token in _UNITS:
        return _UNITS[token]
    return _UNIT_ALIASES.get(token)


# Trailing target tokens that mean "all named panes" for a slash command.
_ALL_TARGETS = ("all", "everyone", "everybody")
# Tokens after a close verb that mean "close every other pane" ("close the
# rest", "close everyone"). Superset of _ALL_TARGETS so close stays consistent
# with the slash all-target grammar; none are valid CALLSIGNS, so this can't
# shadow a real pane name.
_CLOSE_ALL_TARGETS = frozenset(_ALL_TARGETS) | {"others", "rest"}


@dataclass(frozen=True)
class Command:
    # create|macro|close|close_others|focus|swap|zoom|unzoom|slash|broadcast|unknown
    kind: str
    count: int = 0
    program: str | None = None             # None = config default; "" = default shell
    name: str = ""
    name_b: str = ""
    text: str = ""                         # broadcast remainder / slash literal
    actions: tuple[str, ...] = ()          # macro expansion
    raw: str = ""                          # unknown body (for feedback)
    unit: str = "pane"                     # pane|window
    to_all: bool = False                   # slash: target all named panes


def _tokens(s: str) -> list[str]:
    return [t for t in (tok.strip(_STRIP).lower() for tok in s.split()) if t]


def _lead(text: str) -> tuple[str, str]:
    parts = text.strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0].strip(_STRIP).lower(), (parts[1] if len(parts) > 1 else "")


def _parse_create(toks: list[str], programs: dict[str, str]) -> Command | None:
    if toks[:2] == ["spin", "up"]:
        rest = toks[2:]
    elif toks and toks[0] in _CREATE_VERBS:
        rest = toks[1:]
    else:
        return None
    if not rest:
        return None
    n = 1 if rest[0] in _ONE_WORDS else word_to_int(rest[0])
    if n is None or not (1 <= n <= 9):
        return None
    # The trailing unit noun is optional ("create two" == "create two panes").
    # When the last token names a unit, consume it; otherwise default to a pane
    # and treat the remaining tokens as a possible program.
    tail = rest[1:]
    unit = "pane"
    if tail:
        resolved = _resolve_unit(tail[-1])
        if resolved is not None:
            unit = resolved
            tail = tail[:-1]
    mid = [t for t in tail if t not in _CREATE_FILLERS]
    if not mid:
        program: str | None = None
    elif len(mid) == 1 and mid[0] in programs:
        program = programs[mid[0]]
    else:
        return None  # unrecognized program -> falls through to unknown
    return Command(kind="create", count=n, program=program, unit=unit)


def _parse_close(toks: list[str]) -> Command | None:
    if not toks or toks[0] not in _CLOSE_VERBS:
        return None
    rest = [t for t in toks[1:] if t != "the"]
    if not rest:
        return None
    if rest[0] in _CLOSE_ALL_TARGETS:
        return Command(kind="close_others")
    return Command(kind="close", name=rest[0])


def _parse_focus(toks: list[str]) -> Command | None:
    if len(toks) >= 2 and toks[0] == "focus":
        return Command(kind="focus", name=toks[1])
    if len(toks) >= 3 and toks[0] in ("switch", "go") and toks[1] == "to":
        return Command(kind="focus", name=toks[2])
    return None


def _parse_swap(toks: list[str]) -> Command | None:
    if toks and toks[0] == "swap":
        names = [t for t in toks[1:] if t != "and"]
        if len(names) >= 2:
            return Command(kind="swap", name=names[0], name_b=names[1])
    return None


def _parse_zoom(toks: list[str]) -> Command | None:
    if not toks:
        return None
    if toks[0] in _UNZOOM_VERBS or toks[:2] in _UNZOOM_PHRASES:
        return Command(kind="unzoom")
    if toks[0] in _ZOOM_VERBS:
        rest = toks[1:]
    elif toks[:2] == ["full", "screen"]:
        rest = toks[2:]
    else:
        return None
    return Command(kind="zoom", name=rest[0] if rest else "")


def _parse_slash(toks: list[str], slash_commands: dict[str, str]) -> Command | None:
    """`<verb> [target]` where verb is a configured slash command.

    No target -> focused pane; "all"/"everyone"/"everybody" -> all named panes;
    any other token -> that pane name. Returns None when the leading token is not
    a configured slash verb (the caller decides unknown vs fall-through)."""
    if not toks or toks[0] not in slash_commands:
        return None
    literal = slash_commands[toks[0]]
    rest = [t for t in toks[1:] if t != "the"]
    if not rest:
        return Command(kind="slash", text=literal)
    if rest[0] in _ALL_TARGETS:
        return Command(kind="slash", text=literal, to_all=True)
    return Command(kind="slash", text=literal, name=rest[0])


def _parse_body(body: str, macros: dict[str, list[str]],
                programs: dict[str, str],
                slash_commands: dict[str, str]) -> Command | None:
    """Macro match, then the verb chain. Returns None when nothing matches
    (the caller decides whether that is `unknown` or a fall-through)."""
    norm = " ".join(_tokens(body))
    for key, actions in macros.items():
        if " ".join(_tokens(key)) == norm and norm:
            return Command(kind="macro", actions=tuple(actions))
    toks = _tokens(body)
    return (_parse_create(toks, programs) or _parse_close(toks)
            or _parse_focus(toks) or _parse_swap(toks) or _parse_zoom(toks)
            or _parse_slash(toks, slash_commands))


def parse_command(
    text: str, *, broadcast_word: str,
    macros: dict[str, list[str]], programs: dict[str, str],
    slash_commands: dict[str, str] | None = None,
    addressing: str = "button",
) -> Command | None:
    slash_commands = slash_commands or {}
    lead, remainder = _lead(text)
    if addressing == "button":
        # The system key is the control signal; no control word is required.
        if lead == broadcast_word:
            return Command(kind="broadcast", text=remainder.strip())
        cmd = _parse_body(text, macros, programs, slash_commands)
        if cmd is not None:
            return cmd
        # Vocative filler before a verb ("okay focus nova", "um create two
        # panes"): peel up to two fillers and retry the verb parse. Broadcast is
        # deliberately NOT peeled (it fans out to every agent; keep it raw-led).
        # A non-command after peeling still returns None -> route + inject.
        peeled, n = _peel_fillers(text)
        if n:
            return _parse_body(peeled, macros, programs, slash_commands)
        return None
    # keyword mode: single key, no command layer (commands live on the button
    # system key). Only the broadcast word leads; everything else falls through
    # to the router (name addressing) or verbatim focus injection.
    if lead == broadcast_word:
        return Command(kind="broadcast", text=remainder.strip())
    return None


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str


def wrap_agent_command(program: str) -> str:
    """Run `program` inside a shell that survives the program's exit.

    Spawning the agent *as* the pane process means quitting it (e.g. claude's
    `exit`) leaves no process, so tmux closes the pane. Wrapping it so the shell
    re-execs an interactive shell when the agent exits lets the pane fall back to
    a usable terminal instead. Empty `program` is the intentional plain-shell
    default and is returned unchanged. tmux runs a single command argument
    through the shell, so the `;`/`exec`/`$SHELL` expansion all resolve there.
    """
    if not program:
        return program
    return f"{program}; exec ${{SHELL:-/bin/sh}} -i"


def _exec_create(cmd: Command, registry, config, io) -> CommandResult:
    if cmd.unit == "window":
        return CommandResult(False, "creating windows by voice isn't supported yet - try panes")
    focused = registry.focused()
    if focused is None:
        return CommandResult(False, "no focused pane to split")
    target = focused.window_id
    program = config.pane_command if cmd.program is None else cmd.program
    # "" is the intentional plain-shell default. A named program that isn't on
    # PATH would spawn panes that exit immediately, so degrade to a shell (same
    # rule as the initial pane) and tell the user, rather than tiling dead panes.
    note = ""
    if program and shutil.which(shlex.split(program)[0]) is None:
        note = f" ('{program}' not found - opened a shell)"
        program = ""
    used = [p.name for p in registry.panes if p.name != p.id]
    assigned: list[str] = []
    for _ in range(cmd.count):
        name = next_callsign(used, fuzzy_cutoff=config.fuzzy_cutoff)
        if name is None:
            io.select_layout(target, "tiled")
            return CommandResult(
                False, f"callsign pool exhausted - named {len(assigned)} of {cmd.count}")
        new_id = io.split_window(target, wrap_agent_command(program))
        io.set_pane_name(new_id, name)
        used.append(name)
        assigned.append(name)
    io.select_layout(target, "tiled")
    return CommandResult(True, f"created {cmd.count} panes: {' '.join(assigned)}{note}")


def _exec_focus(cmd: Command, registry, config, io) -> CommandResult:
    m = resolve_pane_by_name(cmd.name, registry.panes, fuzzy_cutoff=config.fuzzy_cutoff)
    if m.candidates:
        msg = "ambiguous: " + " / ".join(m.candidates) + " - say the name again"
        return CommandResult(False, msg)
    if m.pane_id is None:
        return CommandResult(False, f"no pane named {cmd.name}")
    io.select_pane(m.pane_id)
    return CommandResult(True, f"focused {m.matched_name}")


def _exec_swap(cmd: Command, registry, config, io) -> CommandResult:
    a = resolve_pane_by_name(cmd.name, registry.panes, fuzzy_cutoff=config.fuzzy_cutoff)
    b = resolve_pane_by_name(cmd.name_b, registry.panes, fuzzy_cutoff=config.fuzzy_cutoff)
    for m, raw in ((a, cmd.name), (b, cmd.name_b)):
        if m.candidates:
            msg = "ambiguous: " + " / ".join(m.candidates) + " - say the name again"
            return CommandResult(False, msg)
        if m.pane_id is None:
            return CommandResult(False, f"no pane named {raw}")
    io.swap_pane(a.pane_id, b.pane_id)
    return CommandResult(True, f"swapped {a.matched_name} <-> {b.matched_name}")


def _exec_close(cmd: Command, registry, config, io) -> CommandResult:
    m = resolve_pane_by_name(cmd.name, registry.panes, fuzzy_cutoff=config.fuzzy_cutoff)
    if m.candidates:
        msg = "ambiguous: " + " / ".join(m.candidates) + " - say the name again"
        return CommandResult(False, msg)
    if m.pane_id is None:
        return CommandResult(False, f"no pane named {cmd.name}")
    io.kill_pane(m.pane_id)
    return CommandResult(True, f"closed {m.matched_name}")


def _exec_close_others(cmd: Command, registry, config, io) -> CommandResult:
    focused = registry.focused()
    if focused is None:
        return CommandResult(False, "no focused pane to keep")
    victims = [p for p in registry.panes if p.id != focused.id]
    if not victims:
        return CommandResult(False, "no other panes to close")
    for p in victims:
        io.kill_pane(p.id)
    kept = focused.name if focused.name != focused.id else "the focused pane"
    return CommandResult(True, f"closed {len(victims)} panes, kept {kept}")


def _exec_zoom(cmd: Command, registry, config, io) -> CommandResult:
    if cmd.name:
        m = resolve_pane_by_name(cmd.name, registry.panes, fuzzy_cutoff=config.fuzzy_cutoff)
        if m.candidates:
            msg = "ambiguous: " + " / ".join(m.candidates) + " - say the name again"
            return CommandResult(False, msg)
        if m.pane_id is None:
            return CommandResult(False, f"no pane named {cmd.name}")
        pane_id, label = m.pane_id, m.matched_name
    else:
        focused = registry.focused()
        if focused is None:
            return CommandResult(False, "no focused pane to zoom")
        pane_id = focused.id
        label = focused.name if focused.name != focused.id else "the focused pane"
    # Select first: selecting a pane in an already-zoomed window unzooms it, so
    # read the flag afterwards and toggle only if not yet zoomed (deterministic).
    io.select_pane(pane_id)
    if not io.pane_zoomed(pane_id):
        io.toggle_zoom(pane_id)
    return CommandResult(True, f"zoomed {label}")


def _exec_unzoom(cmd: Command, registry, config, io) -> CommandResult:
    # Zoom is window-level (one pane per window); unzoom the focused pane's window.
    focused = registry.focused()
    if focused is None:
        return CommandResult(False, "no focused pane")
    if io.pane_zoomed(focused.id):
        io.toggle_zoom(focused.id)
        return CommandResult(True, "unzoomed")
    return CommandResult(True, "already unzoomed")


def _exec_macro(cmd: Command, registry, config, io) -> CommandResult:
    msgs: list[str] = []
    ok = True
    for action in cmd.actions:
        toks = _tokens(action)
        sub = _parse_create(toks, config.programs)
        if sub is not None:
            res = _exec_create(sub, registry, config, io)
            ok = ok and res.ok
            msgs.append(res.message)
        elif toks == ["tile"]:
            focused = registry.focused()
            if focused is not None:
                io.select_layout(focused.window_id, "tiled")
                msgs.append("tiled")
            else:
                ok = False
                msgs.append("tile: no focused pane")
        else:
            ok = False
            msgs.append(f"skipped: {action}")
    return CommandResult(ok, "; ".join(msgs) if msgs else "macro: nothing to do")


def _exec_broadcast(cmd: Command, registry, config, inject_fn) -> CommandResult:
    if not cmd.text.strip():
        return CommandResult(False, "nothing to broadcast")
    targets = [p for p in registry.panes if p.name != p.id]
    if not targets:
        return CommandResult(False, "no named agents to broadcast to")
    ok = 0
    for p in targets:
        if inject_fn(p.id, cmd.text, confirm_timeout=config.inject_confirm_timeout,
                     poll_interval=config.inject_poll_interval):
            ok += 1
    return CommandResult(True, f"broadcast to {ok}/{len(targets)} agents")


def _inject(inject_fn, pane_id, text, config) -> bool:
    return inject_fn(pane_id, text, confirm_timeout=config.inject_confirm_timeout,
                     poll_interval=config.inject_poll_interval)


def _exec_slash(cmd: Command, registry, config, inject_fn) -> CommandResult:
    literal = cmd.text
    if cmd.to_all:
        targets = [p for p in registry.panes if p.name != p.id]
        if not targets:
            return CommandResult(False, "no named agents")
        ok = sum(1 for p in targets if _inject(inject_fn, p.id, literal, config))
        return CommandResult(True, f"sent {literal} to {ok}/{len(targets)} agents")
    if cmd.name:
        m = resolve_pane_by_name(cmd.name, registry.panes, fuzzy_cutoff=config.fuzzy_cutoff)
        if m.candidates:
            msg = "ambiguous: " + " / ".join(m.candidates) + " - say the name again"
            return CommandResult(False, msg)
        if m.pane_id is None:
            return CommandResult(False, f"no pane named {cmd.name}")
        pane_id, label = m.pane_id, m.matched_name
    else:
        focused = registry.focused()
        if focused is None:
            return CommandResult(False, "no focused pane")
        pane_id = focused.id
        label = focused.name if focused.name != focused.id else "the focused pane"
    if _inject(inject_fn, pane_id, literal, config):
        return CommandResult(True, f"sent {literal} to {label}")
    return CommandResult(False, f"failed to send {literal} to {label}")


def execute_command(cmd: Command, registry, config, *,
                    io=tmuxio, inject_fn=inject) -> CommandResult:
    try:
        if cmd.kind == "create":
            return _exec_create(cmd, registry, config, io)
        if cmd.kind == "macro":
            return _exec_macro(cmd, registry, config, io)
        if cmd.kind == "focus":
            return _exec_focus(cmd, registry, config, io)
        if cmd.kind == "swap":
            return _exec_swap(cmd, registry, config, io)
        if cmd.kind == "close":
            return _exec_close(cmd, registry, config, io)
        if cmd.kind == "close_others":
            return _exec_close_others(cmd, registry, config, io)
        if cmd.kind == "zoom":
            return _exec_zoom(cmd, registry, config, io)
        if cmd.kind == "unzoom":
            return _exec_unzoom(cmd, registry, config, io)
        if cmd.kind == "slash":
            return _exec_slash(cmd, registry, config, inject_fn)
        if cmd.kind == "broadcast":
            return _exec_broadcast(cmd, registry, config, inject_fn)
        return CommandResult(False, f"unknown command: {cmd.raw}")
    except TmuxError as exc:
        return CommandResult(False, f"tmux error: {exc}")


def handle_command(text: str, registry, config, *,
                   io=tmuxio, inject_fn=inject,
                   addressing: str = "button") -> CommandResult | None:
    cmd = parse_command(
        text, broadcast_word=config.broadcast_word,
        macros=config.macros, programs=config.programs,
        slash_commands=config.slash_commands, addressing=addressing)
    if cmd is None:
        return None
    return execute_command(cmd, registry, config, io=io, inject_fn=inject_fn)
