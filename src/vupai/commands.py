"""Command layer: interpret control-word utterances into structured Commands.

Interpretation (parse_command) is deliberately separate from execution
(execute_command, Task 6). The Command dataclass is the stable contract and the
seam for a future local-LLM interpreter: it would be another producer of
Commands, escalated to only on kind == "unknown". No LLM here.
"""
from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass

from vupai import tmuxio
from vupai.injector import inject
from vupai.router import (
    _peel_fillers,
    next_callsign,
    resolve_pane_by_name,
    word_to_int,
)
from vupai.tmuxio import TmuxError

_STRIP = ".,!?;:'\"()[]{}"
_CREATE_VERBS = ("create", "make", "add", "open", "new")
# Curated ASR mishearings of the lead verb "create" (ate/hate/eight/crate). Same
# rationale as _UNIT_ALIASES: scoring would over-match, so the set is explicit.
# These ARE real words, but only matter on the button command key (plain dictation
# goes verbatim via the other key) AND the parse still requires a valid 1-9 count
# right after, so a non-create utterance ("hate this code") finds no count and
# falls through to inject - non-destructive. Extend with a one-liner + a test.
_CREATE_VERB_ALIASES = frozenset({"ate", "hate", "eight", "crate", "creator"})
_CLOSE_VERBS = ("close", "kill")
# Curated ASR mishearings of "close" (clothes/cloze/rose - "close nova" lands as
# "rose nova" in the wild). close is DESTRUCTIVE so the set stays tighter than the
# create aliases. "rose" is a real word (the usual precision-guard no), kept here
# only because the blast radius is contained: the parse still requires a target
# after the verb (a bare verb is None), and execution resolves that target to a
# real pane or returns "no pane named ..." - so a misfire can't silently kill a
# pane, and "rose" is not a CALLSIGN so it never shadows an auto-named pane.
# "kill" has no clean homophone -> omitted.
_CLOSE_VERB_ALIASES = frozenset({"clothes", "cloze", "rose"})
_ZOOM_VERBS = ("zoom", "maximize")
# Curated ASR mishearing of "zoom" (zoo). View-only action, so low risk; same
# explicit-set pattern as the other verb aliases.
_ZOOM_VERB_ALIASES = frozenset({"zoo"})
_UNZOOM_VERBS = ("unzoom", "minimize", "restore")
# Parakeet splits "unzoom" into two tokens ("and zoom" / "un zoom"). Curated,
# deterministic - the leading token is implausible as a literal command on its
# own, so matching it here can't shadow a real utterance.
_UNZOOM_PHRASES = (["and", "zoom"], ["un", "zoom"])
# Curated ASR mishearings of "swap" (swab/swamp - b/p, m/p confusion). swap
# rearranges panes, so the explicit set stays tight; the parse also requires two
# name tokens after the verb, so a bare misfire resolves to nothing.
_SWAP_VERBS = ("swap",)
_SWAP_VERB_ALIASES = frozenset({"swab", "swamp"})
_LAYOUT_VERBS = ("layout",)
# Curated mishearing of the lead verb. Kept tight: "layout" transcribes cleanly;
# the two-token split "lay out" is handled separately in _parse_layout. Extend
# with a one-liner + a test when a real mishearing shows up.
_LAYOUT_VERB_ALIASES = frozenset({"layouts"})
# Name-phrase (after the mandatory lead verb) -> (tmux layout, focus-aware main).
# The aliases are real English words; they are SAFE ONLY as the name token(s)
# after the verb, never as a toks[0] verb-alias. NEVER move a key here into a
# toks[0]-matched set. View-only action, so a miss is harmless (falls through to
# dictation). "even" alone is intentionally absent (it would tie-break columns
# vs rows); say the axis. Extend with a one-liner + a test.
_LAYOUTS: dict[str, tuple[str, bool]] = {
    "grid": ("tiled", False),
    "tile": ("tiled", False),
    "tiled": ("tiled", False),
    "tiles": ("tiled", False),
    "bento": ("tiled", False),
    "left": ("main-vertical", True),
    "focus left": ("main-vertical", True),
    "main left": ("main-vertical", True),
    "stack right": ("main-vertical", True),
    "top": ("main-horizontal", True),
    "focus top": ("main-horizontal", True),
    "main top": ("main-horizontal", True),
    "stack bottom": ("main-horizontal", True),
    "columns": ("even-horizontal", False),
    "even columns": ("even-horizontal", False),
    "rows": ("even-vertical", False),
    "even rows": ("even-vertical", False),
}
# tmux layout name -> the word used in the spoken-feedback message. Pinned so the
# message wording is fixed, not re-derived. Mirrors the feedback-label column of
# the design's vocabulary table.
_LAYOUT_LABELS = {
    "tiled": "grid",
    "main-vertical": "main left",
    "main-horizontal": "main top",
    "even-horizontal": "columns",
    "even-vertical": "rows",
}
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


# Curated ASR mishearings of a program token -> canonical key in `programs`.
# Same rationale as _UNIT_ALIASES: deterministic, lists only implausible-as-literal
# mistranscriptions, and contained (the create parse already required a valid 1-9
# count, so a misfire falls through to `unknown`, never injected). "codex" lands as
# "codecs"/"codec" (the reported bug). Single-token aliases only; multi-token
# splits live in _PROGRAM_PHRASE_ALIASES. Extend with a one-liner + a test.
_PROGRAM_ALIASES = {"codecs": "codex", "codec": "codex"}
# Program names the ASR splits into two tokens. "opencode" comes back as the
# literal phrase "open code" - and since "open" is itself a create verb, this can
# never match the single-token program check, so the whole phrase is mapped here.
# Keyed by the token tuple (already lowercased/stripped by _tokens).
_PROGRAM_PHRASE_ALIASES: dict[tuple[str, ...], str] = {("open", "code"): "opencode"}


def _resolve_program(mid: list[str], programs: dict[str, str]) -> str | None:
    """Map the program tokens of a create utterance to a `programs` value, or None.

    Returns the launch string (which may be "" for the default shell) when `mid`
    names a known program directly, via a single-token homophone, or via a
    two-token split phrase; None when `mid` is unrecognized (the caller then
    falls the utterance through to `unknown`). `mid` is non-empty and already
    filler-stripped by the caller.
    """
    phrase = _PROGRAM_PHRASE_ALIASES.get(tuple(mid))
    if phrase is not None:
        return programs.get(phrase)
    if len(mid) == 1:
        key = _PROGRAM_ALIASES.get(mid[0], mid[0])
        if key in programs:
            return programs[key]
    return None


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


# Command kinds that mutate/destroy state irreversibly and so are gated behind a
# confirmation prompt when config.confirm_destructive is on. close/close_others
# kill panes (the process is gone - no undo); broadcast fans text to every agent.
DESTRUCTIVE_KINDS = frozenset({"close", "close_others", "broadcast"})

# Upper bound on panes a single create utterance may spawn. A safety bound so a
# mishearing ("create thirty" misheard for something) can't fan out a runaway
# count; large-but-plausible counts are gated by the confirmation popup instead
# (see config.confirm_create_threshold). Kept <= the CALLSIGNS pool so a
# max-count create from a fresh window can still name every pane.
MAX_CREATE_COUNT = 30


@dataclass(frozen=True)
class Command:
    # create|macro|close|close_others|focus|swap|zoom|unzoom|layout|slash|broadcast|unknown
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
    layout: str = ""                       # tmux layout name (kind == "layout")
    main_focus: bool = False               # layout: swap focused pane into main slot


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
    elif toks and (toks[0] in _CREATE_VERBS or toks[0] in _CREATE_VERB_ALIASES):
        rest = toks[1:]
    else:
        return None
    if not rest:
        return None
    n = 1 if rest[0] in _ONE_WORDS else word_to_int(rest[0])
    if n is None or not (1 <= n <= MAX_CREATE_COUNT):
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
    else:
        program = _resolve_program(mid, programs)
        if program is None:
            return None  # unrecognized program -> falls through to unknown
    return Command(kind="create", count=n, program=program, unit=unit)


def _parse_close(toks: list[str]) -> Command | None:
    if not toks or (toks[0] not in _CLOSE_VERBS and toks[0] not in _CLOSE_VERB_ALIASES):
        return None
    rest = [t for t in toks[1:] if t != "the"]
    if not rest:
        return None
    if rest[0] in _CLOSE_ALL_TARGETS:
        return Command(kind="close_others")
    return Command(kind="close", name=rest[0])


def _parse_focus(toks: list[str]) -> Command | None:
    # Drop a leading "the" from the target ("focus the nova"), matching close/slash.
    if toks and toks[0] == "focus":
        rest = [t for t in toks[1:] if t != "the"]
        if rest:
            return Command(kind="focus", name=rest[0])
        return None
    if len(toks) >= 3 and toks[0] in ("switch", "go") and toks[1] == "to":
        rest = [t for t in toks[2:] if t != "the"]
        if rest:
            return Command(kind="focus", name=rest[0])
    return None


def _parse_swap(toks: list[str]) -> Command | None:
    if toks and (toks[0] in _SWAP_VERBS or toks[0] in _SWAP_VERB_ALIASES):
        names = [t for t in toks[1:] if t not in ("and", "the")]
        if len(names) >= 2:
            return Command(kind="swap", name=names[0], name_b=names[1])
    return None


def _parse_zoom(toks: list[str]) -> Command | None:
    if not toks:
        return None
    if toks[0] in _UNZOOM_VERBS or toks[:2] in _UNZOOM_PHRASES:
        return Command(kind="unzoom")
    if toks[0] in _ZOOM_VERBS or toks[0] in _ZOOM_VERB_ALIASES:
        rest = toks[1:]
    elif toks[:2] == ["full", "screen"]:
        rest = toks[2:]
    else:
        return None
    rest = [t for t in rest if t != "the"]  # "zoom the nova" -> nova
    return Command(kind="zoom", name=rest[0] if rest else "")


def _parse_layout(toks: list[str]) -> Command | None:
    """`layout <name>` (or `lay out <name>`) -> a layout Command, else None.

    The lead verb is mandatory; the trailing name-phrase is looked up in
    _LAYOUTS. An unknown name (or a bare verb) returns None so the utterance
    falls through to dictation - never destructive.
    """
    if toks and (toks[0] in _LAYOUT_VERBS or toks[0] in _LAYOUT_VERB_ALIASES):
        rest = toks[1:]
    elif toks[:2] == ["lay", "out"]:
        rest = toks[2:]
    else:
        return None
    rest = [t for t in rest if t != "the"]
    if not rest:
        return None
    hit = _LAYOUTS.get(" ".join(rest))
    if hit is None:
        return None
    layout, main_focus = hit
    return Command(kind="layout", layout=layout, main_focus=main_focus)


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
            or _parse_layout(toks)
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


def program_label(program: str) -> str:
    """The short program name shown on the pane border (e.g. "claude").

    Reduces a program string ("claude", "/usr/bin/codex --foo", "") to the
    basename of its first token. Empty for the plain-shell default, so the
    border simply omits the program segment.
    """
    program = program.strip()
    if not program:
        return ""
    return os.path.basename(shlex.split(program)[0])


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
        io.set_pane_program(new_id, program_label(program))
        used.append(name)
        assigned.append(name)
        # Re-tile after every split: splitting always shrinks the active pane,
        # so without redistributing space tmux runs out ("no space for new pane")
        # around the 6th-7th split. Tiling each round keeps room for the next.
        io.select_layout(target, "tiled")
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
    # Scope to the focused pane's session: the registry is server-wide
    # (list-panes -a, for name routing), but "close all/others" must stay inside
    # the current repo's session so it can't kill another repo's panes.
    victims = [p for p in registry.panes
               if p.id != focused.id and p.session == focused.session]
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


def _exec_layout(cmd: Command, registry, config, io) -> CommandResult:
    focused = registry.focused()
    if focused is None:
        return CommandResult(False, "no focused pane")
    # Window-scoped. window_id is implicitly single-session (tmux @N ids are
    # server-unique and a window belongs to one session), so no session filter
    # is needed here, unlike the name-keyed bulk ops.
    window = [p for p in registry.panes if p.window_id == focused.window_id]
    if len(window) <= 1:
        return CommandResult(True, "only one pane - nothing to arrange")
    if cmd.main_focus:
        # tmux promotes the lowest-index pane to the main slot; swap the focused
        # pane there (detached, so it stays focused) before applying the layout.
        main = min(window, key=lambda p: p.index)
        if focused.id != main.id:
            io.swap_pane(focused.id, main.id, detached=True)
    # select-layout auto-unzooms, so no explicit unzoom is needed.
    io.select_layout(focused.window_id, cmd.layout)
    return CommandResult(True, f"layout {_LAYOUT_LABELS[cmd.layout]}")


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
    focused = registry.focused()
    if focused is None:
        return CommandResult(False, "no focused pane to scope the broadcast")
    # Stay inside the focused session (see _exec_close_others): broadcast hits
    # this repo's agents, not every agent on the server.
    targets = [p for p in registry.panes
               if p.name != p.id and p.session == focused.session]
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
        focused = registry.focused()
        if focused is None:
            return CommandResult(False, "no focused pane to scope the command")
        # Session-scoped like broadcast/close-others: a slash to "all" targets
        # this repo's agents, not the whole server.
        targets = [p for p in registry.panes
                   if p.name != p.id and p.session == focused.session]
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
        if cmd.kind == "layout":
            return _exec_layout(cmd, registry, config, io)
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
