"""Command layer: interpret control-word utterances into structured Commands.

Interpretation (parse_command) is deliberately separate from execution
(execute_command, Task 6). The Command dataclass is the stable contract and the
seam for a future local-LLM interpreter: it would be another producer of
Commands, escalated to only on kind == "unknown". No LLM here.
"""
from __future__ import annotations

from dataclasses import dataclass

from voxpane.router import word_to_int

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


def parse_command(text, *, control_word, broadcast_word, macros, programs) -> Command | None:
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
    for parser in (_parse_create, _parse_close, _parse_focus, _parse_swap):
        if parser is _parse_create:
            cmd = parser(toks, programs)
        else:
            cmd = parser(toks)
        if cmd is not None:
            return cmd
    return Command(kind="unknown", raw=body)
