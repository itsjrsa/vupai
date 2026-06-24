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

from vupai import board, speech, summarize, tmuxio
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
    # create|macro|close|close_others|focus|swap|zoom|unzoom|layout|board|read|
    # talkback|slash|broadcast|unknown
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
    enable: bool = False                   # talkback: True = unmute, False = mute


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


# Lead verbs that may precede "board" ("open board", "create board", "show
# board"). Bare "board" works too. The create verbs already fail _parse_create
# on "<verb> board" (no count follows), so they fall through to here cleanly.
_BOARD_LEAD = frozenset(_CREATE_VERBS) | {"show"}


def _parse_board(toks: list[str]) -> Command | None:
    """`board` / `<open|create|show> board` -> open the supervision board."""
    rest = [t for t in toks if t != "the"]
    if rest == ["board"]:
        return Command(kind="board")
    if len(rest) == 2 and rest[1] == "board" and rest[0] in _BOARD_LEAD:
        return Command(kind="board")
    return None


_READ_VERBS = ("read",)
# Curated ASR mishearings of "read": the homophone "reed" and the past-tense
# spelling "red". Reading only SPEAKS a summary (no state change), so a misfire is
# harmless - at worst it reads the focused pane - but the set stays tight per the
# alias convention. "read" itself transcribes cleanly.
_READ_VERB_ALIASES = frozenset({"reed", "red"})


def _parse_read(toks: list[str]) -> Command | None:
    """`read [name|board|all]` -> speak a summary aloud.

    Strips a leading article/filler after the verb ("read me nova", "read the
    atlas", "read out nova"). A bare "read" targets the focused pane; "read board"
    (or "read all") speaks a board-style digest of every agent.
    """
    if not toks or (toks[0] not in _READ_VERBS and toks[0] not in _READ_VERB_ALIASES):
        return None
    rest = [t for t in toks[1:] if t not in ("the", "me", "out")]
    # "read board" / "read all" -> a spoken digest of every agent, not a pane named
    # "board" (the visual board pane need not even be open). to_all carries it.
    if rest and (rest[0] == "board" or rest[0] in _ALL_TARGETS):
        return Command(kind="read", to_all=True)
    return Command(kind="read", name=rest[0] if rest else "")


# Talk-back toggle: silence/restore ALL spoken feedback (command acks + read).
# Phrase sets are matched on the normalized full utterance (the system key makes
# the whole utterance a command), so common words like "quiet"/"speak" are safe
# here - plain dictation goes verbatim via the other key. Keyed by the joined,
# article-stripped token string. Disjoint from every other verb set, so ordering
# in the parse chain is irrelevant. Extend with a one-liner + a test.
_TALKBACK_OFF = frozenset({
    "mute", "quiet", "silence", "hush", "shush", "be quiet", "stay quiet",
    "stop talking", "stop talk", "stop speaking", "stop reading", "stop reading back",
    "shut up", "talk back off", "stop talk back", "stop the talk back", "no talk back",
})
_TALKBACK_ON = frozenset({
    "unmute", "speak", "speak up", "talk back", "talk back on", "talk to me",
    "start talking", "read back on", "read backs on", "talk backs on",
})


def _parse_talkback(toks: list[str]) -> Command | None:
    """`mute`/`unmute` (and synonyms) -> toggle all spoken feedback, else None.

    Matched on the whole utterance so it never shadows a pane action; an
    unrecognized phrase returns None and falls through to dictation."""
    phrase = " ".join(t for t in toks if t not in ("the", "please"))
    if phrase in _TALKBACK_OFF:
        return Command(kind="talkback", enable=False)
    if phrase in _TALKBACK_ON:
        return Command(kind="talkback", enable=True)
    return None


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
    # `read` is parsed AFTER slash so a user-configured slash verb named "read"
    # (or "red"/"reed") still wins - the built-in read never silently shadows a
    # configured slash command. Default config has no such collision.
    return (_parse_create(toks, programs) or _parse_close(toks)
            or _parse_focus(toks) or _parse_swap(toks) or _parse_zoom(toks)
            or _parse_layout(toks) or _parse_board(toks) or _parse_talkback(toks)
            or _parse_slash(toks, slash_commands) or _parse_read(toks))


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
    # Optional natural-language phrase for spoken talk-back. The status-line
    # `message` is terse and symbol-laden ("swapped a <-> b", "sent /clear to nova",
    # "2/2 agents") which reads badly aloud; `spoken` is the say-friendly twin.
    # Empty means "speak the message" (word-only messages read fine as-is).
    spoken: str = ""


def intent_phrase(cmd: Command) -> str:
    """Present-tense spoken phrase voiced the INSTANT a command is recognized,
    before the confirm popup and execution - so feedback feels immediate instead
    of trailing the (popup-gated, sometimes slow) action. Pure: it reads only the
    parsed Command, never tmux. Empty string -> nothing is voiced up front (read /
    talkback / macro carry their own feedback). The result ack (post-execute) then
    speaks only on failure, so a success is just this one immediate phrase."""
    if cmd.kind == "create":
        return "opening an agent" if cmd.count == 1 else f"opening {cmd.count} agents"
    if cmd.kind == "close":
        return f"closing {cmd.name}"
    if cmd.kind == "close_others":
        return "closing the other agents"
    if cmd.kind == "focus":
        return f"switching to {cmd.name}"
    if cmd.kind == "swap":
        return f"swapping {cmd.name} and {cmd.name_b}"
    if cmd.kind == "zoom":
        return f"zooming {cmd.name}" if cmd.name else "zooming"
    if cmd.kind == "unzoom":
        return "restoring the layout"
    if cmd.kind == "layout":
        return f"{_LAYOUT_LABELS.get(cmd.layout, 'arranging the')} layout"
    if cmd.kind == "board":
        return "opening the board"
    if cmd.kind == "broadcast":
        return "broadcasting"
    if cmd.kind == "slash":
        return f"sending {cmd.text.lstrip('/')}"
    return ""


def talkback_message(enable: bool) -> str:
    """Status-line text for a talk-back toggle. Single source of truth shared by
    the executor and the daemon so the wording can't drift."""
    return "talk-back on" if enable else "talk-back off (muted)"


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
    if len(assigned) == 1:
        spoken = f"{assigned[0]} is up"
    else:
        spoken = f"{len(assigned)} agents up: {', '.join(assigned)}"
    return CommandResult(True, f"created {cmd.count} panes: {' '.join(assigned)}{note}",
                         spoken=spoken)


def _exec_focus(cmd: Command, registry, config, io) -> CommandResult:
    m = resolve_pane_by_name(cmd.name, registry.panes, fuzzy_cutoff=config.fuzzy_cutoff)
    if m.candidates:
        msg = "ambiguous: " + " / ".join(m.candidates) + " - say the name again"
        return CommandResult(False, msg)
    if m.pane_id is None:
        return CommandResult(False, f"no pane named {cmd.name}")
    io.select_pane(m.pane_id)
    return CommandResult(True, f"focused {m.matched_name}",
                         spoken=f"switched to {m.matched_name}")


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
    return CommandResult(True, f"swapped {a.matched_name} <-> {b.matched_name}",
                         spoken=f"swapped {a.matched_name} and {b.matched_name}")


def _exec_close(cmd: Command, registry, config, io) -> CommandResult:
    m = resolve_pane_by_name(cmd.name, registry.panes, fuzzy_cutoff=config.fuzzy_cutoff)
    if m.candidates:
        msg = "ambiguous: " + " / ".join(m.candidates) + " - say the name again"
        return CommandResult(False, msg)
    if m.pane_id is None:
        return CommandResult(False, f"no pane named {cmd.name}")
    io.kill_pane(m.pane_id)
    return CommandResult(True, f"closed {m.matched_name}", spoken=f"closed {m.matched_name}")


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
    return CommandResult(True, f"closed {len(victims)} panes, kept {kept}",
                         spoken=f"closed {len(victims)} agents, kept {kept}")


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


def _exec_board(cmd: Command, registry, config, io) -> CommandResult:
    focused = registry.focused()
    if focused is None:
        return CommandResult(False, "no focused pane to attach the board to")
    # open_board is one-per-session: it focuses an existing board instead of
    # splitting a second one. Both outcomes are a success for the speaker.
    _, message = board.open_board(focused.id, focused.session, io=io)
    return CommandResult(True, message)


# Read bounds the captured scrollback before summarizing, same budget as the
# board's tail (board._CAPTURE_LINES / _TAIL_BYTES): enough for a conclusion,
# capped so a long-running pane can't blow up the summarizer's input.
_READ_CAPTURE_LINES = 40
_READ_TAIL_BYTES = 6000


def _bound_tail(text: str) -> str:
    """Last N lines then last M UTF-8 bytes of `text` (mirrors board._bounded)."""
    tail = "\n".join(text.splitlines()[-_READ_CAPTURE_LINES:])
    raw = tail.encode("utf-8", "replace")
    if len(raw) > _READ_TAIL_BYTES:
        tail = raw[-_READ_TAIL_BYTES:].decode("utf-8", "replace")
    return tail


def _default_summarizer(config):
    """Spoken read-back summary: richer than the board line and grounded in the
    pane title; reuses the board summarizer command + timeout."""
    return lambda tail, title: summarize.summarize_read(
        tail, cmd=config.board_summarizer_cmd,
        timeout=config.board_summary_timeout_s, title=title)


def _default_speaker(config):
    """A speak callable from config, or a no-op when TTS is off/unset.

    Returns the process handle (or None) so a streaming SentenceSpeaker can wait
    on each utterance; the non-streaming path ignores the return."""
    if not config.tts_enabled or not config.tts_cmd:
        return lambda _text: None
    return lambda text: speech.speak(text, cmd=config.tts_cmd)


def _default_stream_summarizer(config):
    """Streaming read summary bound to config; feeds spoken text to `on_text`."""
    return lambda tail, title, on_text: summarize.summarize_read_stream(
        tail, cmd=config.board_summarizer_cmd,
        timeout=config.board_summary_timeout_s, title=title, on_text=on_text)


def _default_board_statuses(config):
    """Build a board snapshot fn bound to config's one-line board summarizer."""
    return lambda panes: board.collect_statuses(
        panes,
        summarize_fn=lambda tail: summarize.summarize(
            tail, cmd=config.board_summarizer_cmd,
            timeout=config.board_summary_timeout_s))


def _exec_read_board(registry, config, io, *, speak_fn=None,
                     statuses_fn=None) -> CommandResult:
    """Speak a board-style digest of every agent (state + one-line summary).

    Independent of the visual board pane: it snapshots the focused session's named
    agent panes directly (excluding the board pane itself), so "read board" works
    whether or not a board pane is open. Read-only, so it runs safely on the read
    worker thread; the digest is also the CommandResult message (surfaces with TTS
    off). statuses_fn is injected by the unit suite (no tmux, no subprocess).
    """
    focused = registry.focused()
    session = focused.session if focused is not None else ""
    try:
        board_id = io.find_board_pane(session) if session else None
    except Exception:
        board_id = None
    panes = [p for p in registry.panes
             if p.name != p.id and p.id != board_id
             and (not session or p.session == session)]
    if statuses_fn is None and config.tts_stream:
        return _exec_read_board_stream(panes, config, speak_fn=speak_fn)
    speak_fn = speak_fn or _default_speaker(config)
    statuses_fn = statuses_fn or _default_board_statuses(config)
    try:
        statuses = statuses_fn(panes)
    except Exception:
        return CommandResult(False, "couldn't read the board")
    spoken = board.speak_statuses(statuses)
    try:
        speak_fn(spoken)
    except Exception:
        pass  # TTS is best-effort; the digest still surfaces as the message
    return CommandResult(True, spoken)


def _exec_read_board_stream(panes, config, *, speak_fn=None) -> CommandResult:
    """Streaming "read board": speak the header now, each agent as it lands.

    The opening "N agents on the board." is voiced immediately (using the count
    of agent panes) while the per-pane one-line summaries run concurrently; each
    agent's clause is then spoken in pane order the moment its summary lands, via
    board.collect_statuses' on_status hook feeding a SentenceSpeaker. The full
    digest is still returned for the status line. Best-effort: failures close the
    speaker and report, never raise (runs on the read worker thread).
    """
    speak_fn = speak_fn or _default_speaker(config)
    speaker = speech.SentenceSpeaker(speak_one=speak_fn)
    if not panes:
        speaker.feed("No agents to report.")
        speaker.close()
        return CommandResult(True, "No agents to report.")
    speaker.feed(board.status_header(len(panes)) + " ")
    statuses: list = []

    def _on_status(st):
        statuses.append(st)
        speaker.feed(board.status_clause(st) + " ")

    try:
        board.collect_statuses(
            panes,
            summarize_fn=lambda tail: summarize.summarize(
                tail, cmd=config.board_summarizer_cmd,
                timeout=config.board_summary_timeout_s),
            on_status=_on_status)
    except Exception:
        speaker.close()
        return CommandResult(False, "couldn't read the board")
    speaker.close()
    return CommandResult(True, board.speak_statuses(statuses))


def _resolve_read_target(cmd, registry, config):
    """Resolve the read target to (pane_id, label) or (None, error message).

    A named pane resolves by name (ambiguous/unknown -> error); a bare "read"
    targets the focused pane, and an unnamed focused pane (name == id) speaks
    without a callsign prefix (label "").
    """
    if cmd.name:
        m = resolve_pane_by_name(cmd.name, registry.panes, fuzzy_cutoff=config.fuzzy_cutoff)
        if m.candidates:
            return None, "ambiguous: " + " / ".join(m.candidates) + " - say the name again"
        if m.pane_id is None:
            return None, f"no pane named {cmd.name}"
        return (m.pane_id, m.matched_name), None
    focused = registry.focused()
    if focused is None:
        return None, "no focused pane to read"
    label = focused.name if focused.name != focused.id else ""
    return (focused.id, label), None


def _exec_read(cmd: Command, registry, config, io, *, capture_fn=None,
               summarize_fn=None, speak_fn=None, title_fn=None,
               statuses_fn=None, stream_fn=None) -> CommandResult:
    """Resolve a pane (named, else focused), summarize its tail, speak it.

    Read-only: it captures + summarizes + speaks, never injects or mutates tmux,
    so it is safe to run on a background thread (the daemon does, off the main
    pipeline). The summary is grounded in the pane title (what the pane is about);
    the spoken line is also the CommandResult message, so it surfaces on the status
    line even with TTS off. Collaborators are injected for the unit suite (no real
    CLI / no audio). summarize_fn takes (tail, title).

    With config.tts_stream on and no summarize_fn injected (the real run), this
    takes the streaming path (_exec_read_stream): speak sentence-by-sentence as
    the summary is generated. An injected summarize_fn forces the original
    one-shot path - the unit suite drives that, deterministic and audio-free.
    """
    if cmd.to_all:  # "read board" / "read all": a digest of every agent
        return _exec_read_board(registry, config, io, speak_fn=speak_fn,
                                statuses_fn=statuses_fn)
    target, err = _resolve_read_target(cmd, registry, config)
    if err is not None:
        return CommandResult(False, err)
    pane_id, label = target
    capture_fn = capture_fn or tmuxio.capture_pane
    title_fn = title_fn or tmuxio.pane_title
    if summarize_fn is None and config.tts_stream:
        return _exec_read_stream(pane_id, label, config, capture_fn, title_fn,
                                 speak_fn=speak_fn, stream_fn=stream_fn)
    summarize_fn = summarize_fn or _default_summarizer(config)
    speak_fn = speak_fn or _default_speaker(config)
    try:
        tail = _bound_tail(capture_fn(pane_id))
        summary = summarize_fn(tail, title_fn(pane_id))
    except Exception:
        # Read is read-only and runs on a worker thread (daemon._run_read), so it
        # must never raise. The default capture/summarize degrade internally; this
        # also guards a custom summarize_fn/title_fn that raises.
        return CommandResult(False, f"couldn't read {label or 'the focused pane'}")
    spoken = f"{label}: {summary.text}" if label else summary.text
    try:
        speak_fn(spoken)
    except Exception:
        pass  # TTS is best-effort; the summary still surfaces as the message
    return CommandResult(True, spoken)


def _exec_read_stream(pane_id, label, config, capture_fn, title_fn, *,
                      speak_fn=None, stream_fn=None) -> CommandResult:
    """Streaming read: speak each sentence as the summary is generated.

    A SentenceSpeaker plays sentences in order (waiting on each `say` so they
    never overlap) while later sentences are still being produced; the label
    rides into the first sentence ("nova: ..."). The full cleaned reply is still
    returned as the message for the status line. Best-effort: any failure closes
    the speaker and reports, never raises (this runs on the read worker thread).
    """
    speak_fn = speak_fn or _default_speaker(config)
    stream_fn = stream_fn or _default_stream_summarizer(config)
    speaker = speech.SentenceSpeaker(speak_one=speak_fn)
    if label:
        speaker.feed(f"{label}: ")
    try:
        tail = _bound_tail(capture_fn(pane_id))
        summary = stream_fn(tail, title_fn(pane_id), speaker.feed)
    except Exception:
        speaker.close()
        return CommandResult(False, f"couldn't read {label or 'the focused pane'}")
    speaker.close()
    spoken = f"{label}: {summary.text}" if label else summary.text
    return CommandResult(True, spoken)


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
    return CommandResult(True, f"broadcast to {ok}/{len(targets)} agents",
                         spoken=f"broadcast to {ok} of {len(targets)} agents")


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
        return CommandResult(True, f"sent {literal} to {ok}/{len(targets)} agents",
                             spoken=f"sent {literal.lstrip('/')} to {ok} of {len(targets)} agents")
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
        return CommandResult(True, f"sent {literal} to {label}",
                             spoken=f"sent {literal.lstrip('/')} to {label}")
    return CommandResult(False, f"failed to send {literal} to {label}")


def execute_command(cmd: Command, registry, config, *,
                    io=tmuxio, inject_fn=inject,
                    capture_fn=None, summarize_fn=None,
                    speak_fn=None, title_fn=None, statuses_fn=None,
                    stream_fn=None) -> CommandResult:
    # capture_fn/summarize_fn/speak_fn/title_fn/statuses_fn/stream_fn are read-command
    # seams (None -> built from config); only the read path consumes them. Mirrors the
    # inject_fn seam, which only the slash/broadcast paths consume.
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
        if cmd.kind == "board":
            return _exec_board(cmd, registry, config, io)
        if cmd.kind == "talkback":
            # The runtime mute flag lives on the daemon (it survives across
            # utterances); this just reports the toggle. "on" confirms aloud, "off"
            # has no spoken twin - by the time it would speak, talk-back is muted.
            return CommandResult(True, talkback_message(cmd.enable),
                                 spoken="talk back on" if cmd.enable else "")
        if cmd.kind == "read":
            return _exec_read(cmd, registry, config, io, capture_fn=capture_fn,
                              summarize_fn=summarize_fn, speak_fn=speak_fn,
                              title_fn=title_fn, statuses_fn=statuses_fn,
                              stream_fn=stream_fn)
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
