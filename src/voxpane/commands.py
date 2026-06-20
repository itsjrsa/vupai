"""Command layer: interpret control-word utterances into structured Commands.

Interpretation (parse_command) is deliberately separate from execution
(execute_command, Task 6). The Command dataclass is the stable contract and the
seam for a future local-LLM interpreter: it would be another producer of
Commands, escalated to only on kind == "unknown". No LLM here.
"""
from __future__ import annotations

from dataclasses import dataclass

from voxpane import tmuxio
from voxpane.injector import inject
from voxpane.router import next_callsign, word_to_int
from voxpane.tmuxio import TmuxError

_STRIP = ".,!?;:'\"()[]{}"
_CREATE_VERBS = ("create", "make", "add", "open", "new")
_CLOSE_VERBS = ("close", "kill")
_UNITS = {"pane": "pane", "panes": "pane", "window": "window", "windows": "window"}


@dataclass(frozen=True)
class Command:
    kind: str                              # create|macro|close|focus|swap|broadcast|unknown
    count: int = 0
    program: str | None = None             # None = config default; "" = default shell
    name: str = ""
    name_b: str = ""
    text: str = ""                         # broadcast remainder (original casing)
    actions: tuple[str, ...] = ()          # macro expansion
    raw: str = ""                          # unknown body (for feedback)
    unit: str = "pane"                     # pane|window


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
    if len(rest) < 2:
        return None
    n = word_to_int(rest[0])
    if n is None or not (1 <= n <= 9):
        return None
    if rest[-1] not in _UNITS:
        return None
    unit = _UNITS[rest[-1]]
    mid = rest[1:-1]
    if not mid:
        program: str | None = None
    elif len(mid) == 1 and mid[0] in programs:
        program = programs[mid[0]]
    else:
        return None  # unrecognized program -> falls through to unknown
    return Command(kind="create", count=n, program=program, unit=unit)


def _parse_close(toks: list[str]) -> Command | None:
    if len(toks) >= 2 and toks[0] in _CLOSE_VERBS:
        return Command(kind="close", name=toks[1])
    return None


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


def parse_command(
    text: str, *, control_word: str, broadcast_word: str,
    macros: dict[str, list[str]], programs: dict[str, str],
) -> Command | None:
    lead, remainder = _lead(text)
    if lead == broadcast_word:
        return Command(kind="broadcast", text=remainder.strip())
    if lead != control_word:
        return None
    body = remainder.strip()
    norm = " ".join(_tokens(body))
    for key, actions in macros.items():
        if " ".join(_tokens(key)) == norm and norm:
            return Command(kind="macro", actions=tuple(actions))
    toks = _tokens(body)
    # Explicit chain (not a loop over the parsers): _parse_create takes an extra
    # `programs` arg, so a single loop variable would have mismatched signatures
    # and trip the type checker. `or` short-circuits on the first Command (always
    # truthy); None falls through.
    cmd = (_parse_create(toks, programs) or _parse_close(toks)
           or _parse_focus(toks) or _parse_swap(toks))
    if cmd is not None:
        return cmd
    return Command(kind="unknown", raw=body)


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str


def _exec_create(cmd: Command, registry, config, io) -> CommandResult:
    if cmd.unit == "window":
        return CommandResult(False, "creating windows by voice isn't supported yet - try panes")
    focused = registry.focused()
    if focused is None:
        return CommandResult(False, "no focused pane to split")
    target = focused.window_id
    program = config.pane_command if cmd.program is None else cmd.program
    used = [p.name for p in registry.panes if p.name != p.id]
    assigned: list[str] = []
    for _ in range(cmd.count):
        name = next_callsign(used, fuzzy_cutoff=config.fuzzy_cutoff)
        if name is None:
            io.select_layout(target, "tiled")
            return CommandResult(
                False, f"callsign pool exhausted - named {len(assigned)} of {cmd.count}")
        new_id = io.split_window(target, program)
        io.set_pane_name(new_id, name)
        used.append(name)
        assigned.append(name)
    io.select_layout(target, "tiled")
    return CommandResult(True, f"created {cmd.count} panes: {' '.join(assigned)}")


def _exec_macro(cmd: Command, registry, config, io) -> CommandResult:
    msgs: list[str] = []
    for action in cmd.actions:
        toks = _tokens(action)
        sub = _parse_create(toks, config.programs)
        if sub is not None:
            msgs.append(_exec_create(sub, registry, config, io).message)
        elif toks == ["tile"]:
            focused = registry.focused()
            if focused is not None:
                io.select_layout(focused.window_id, "tiled")
                msgs.append("tiled")
        else:
            msgs.append(f"skipped: {action}")
    return CommandResult(True, "; ".join(msgs) if msgs else "macro: nothing to do")


def execute_command(cmd: Command, registry, config, *,
                    io=tmuxio, inject_fn=inject) -> CommandResult:
    try:
        if cmd.kind == "create":
            return _exec_create(cmd, registry, config, io)
        if cmd.kind == "macro":
            return _exec_macro(cmd, registry, config, io)
        return CommandResult(False, f"unknown command: {cmd.raw}")
    except TmuxError as exc:
        return CommandResult(False, f"tmux error: {exc}")


def handle_command(text: str, registry, config, *,
                   io=tmuxio, inject_fn=inject) -> CommandResult | None:
    cmd = parse_command(
        text, control_word=config.control_word, broadcast_word=config.broadcast_word,
        macros=config.macros, programs=config.programs)
    if cmd is None:
        return None
    return execute_command(cmd, registry, config, io=io, inject_fn=inject_fn)
