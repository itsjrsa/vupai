"""Swappable, best-effort pane summarizer for the supervision board.

`summarize` shells out to `board_summarizer_cmd` (default `claude -p ...`),
passing a fixed tool-neutral instruction plus the pane's scrollback tail as ONE
argv argument (never stdin, never a shell), and reads back the LAST non-blank
line of stdout. That single rule is the agnostic lowest common denominator:
`claude`, `codex exec`, `gemini -p`, and `ollama run` all satisfy it unmodified,
and it neutralizes tools that interleave an event trace or print a banner.

Every failure path - command not found, nonzero exit, timeout, empty output -
degrades to a pure-stdlib fallback (the last meaningful line of scrollback), so
the board always renders something and no single tool is ever load-bearing.
Mirrors watcher._osascript_notify: best-effort, exceptions swallowed.
"""
from __future__ import annotations

import logging
import re
import shlex
import subprocess
from dataclasses import dataclass

from vupai.panestate import detect_needs_input

logger = logging.getLogger(__name__)

# Tool-neutral, imperative, short. Names no specific tool and assumes no output
# format beyond "one line". The NEEDS: convention lets any backend flag a prompt.
_INSTRUCTION = (
    "Summarize the state of this terminal pane for a supervision dashboard. "
    "Output ONE line, max 100 characters: the main conclusion or the pending "
    "action/question. No preamble, no markdown, no quotes. If the pane is "
    "waiting for the user to answer, start the line with 'NEEDS: '."
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_NEEDS_PREFIX = "needs:"
_FALLBACK_MAX = 90


@dataclass
class Summary:
    """One pane's summary plus whether it appears to await input."""
    text: str
    needs_input: bool
    source: str  # "llm" when the summarizer produced it, "fallback" otherwise


def build_prompt(tail: str) -> str:
    """The single argv argument: instruction followed by the scrollback tail."""
    return f"{_INSTRUCTION}\n\n--- pane scrollback (tail) ---\n{tail}"


def _clean(text: str) -> str:
    """Strip ANSI escapes and collapse whitespace to a single line."""
    return " ".join(_ANSI_RE.sub("", text).split())


def _last_nonblank_line(stdout: str) -> str:
    """Last stdout line that is non-empty AFTER ANSI/whitespace cleaning.

    Cleaning before the emptiness test matters: a trailing escape-only line
    (e.g. a cursor-show or color-reset some tools print) is non-blank to
    str.strip() but cleans to "", and selecting it would mask the real summary
    sitting one line above.
    """
    for line in reversed(stdout.splitlines()):
        cleaned = _clean(line)
        if cleaned:
            return cleaned
    return ""


def _fallback(tail: str) -> Summary:
    """Non-LLM summary: the last meaningful line of scrollback."""
    needs = detect_needs_input(tail)
    lines = [c for c in (_clean(ln) for ln in tail.splitlines()) if c]
    if not lines:
        return Summary("(no output)", needs, "fallback")
    return Summary(lines[-1][:_FALLBACK_MAX], needs, "fallback")


def summarize(tail: str, *, cmd: str, timeout: float = 12.0,
              max_chars: int = 100, runner=subprocess.run) -> Summary:
    """Summarize `tail` via `cmd`, degrading to the stdlib fallback on any failure."""
    argv = shlex.split(cmd)
    if not argv:
        return _fallback(tail)
    argv.append(build_prompt(tail))
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        logger.debug("summarizer command not found: %s", argv[0])
        return _fallback(tail)
    except subprocess.TimeoutExpired:
        logger.debug("summarizer timed out after %ss", timeout)
        return _fallback(tail)
    except Exception:
        logger.debug("summarizer failed to run", exc_info=True)
        return _fallback(tail)

    if proc.returncode != 0:
        logger.debug("summarizer exited %s", proc.returncode)
        return _fallback(tail)

    line = _last_nonblank_line(proc.stdout or "")  # already ANSI-cleaned
    needs = line[:len(_NEEDS_PREFIX)].lower() == _NEEDS_PREFIX
    if needs:
        line = line[len(_NEEDS_PREFIX):].strip()
    line = line[:max_chars].strip()
    if not line:
        # Empty / NEEDS-only output carries no conclusion; fall back but keep the
        # needs-input signal the model (or the tail) gave us.
        fb = _fallback(tail)
        return Summary(fb.text, needs or fb.needs_input, "fallback")
    return Summary(line, needs, "llm")
