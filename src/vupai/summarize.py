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

# Tool-neutral, imperative, short. Names no specific tool. Steers the model to
# the SUBSTANCE of the work (what was produced / asked) rather than the pane's UI
# state, and to ignore the input box / status timers / shortcut footer that sit
# at the bottom of a coding-agent TUI and would otherwise dominate by recency.
_INSTRUCTION = (
    "Below is the recent terminal output of an AI coding agent. In ONE short "
    "line (max 100 characters), say what the agent most recently produced or is "
    "being asked to do - the substance of the work, not the UI state. Ignore "
    "input boxes, status timers, token counts, and keyboard-shortcut or footer "
    "hints. No preamble, no markdown, no quotes. If the agent is waiting for the "
    "user to answer a question, start the line with 'NEEDS: '."
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_NEEDS_PREFIX = "needs:"
_FALLBACK_MAX = 90

# TUI affordances, not work content. Conservative: a line is only dropped when
# it is CLEARLY chrome, never when it carries a real sentence, so the substance
# (including a pending request typed at the prompt) always survives. Helps the
# model (less bottom-of-screen recency noise) and the fallback (won't echo a
# footer like "auto mode on (shift+tab to cycle)"). The substrings are common
# coding-agent footers; unknown tools' chrome simply passes through (harmless).
_CHROME_SUBSTRINGS = (
    "shift+tab to cycle",
    "? for shortcuts",
    "for agents",
    "auto mode on",
    "auto mode off",
    "esc to interrupt",
)
# A lone status/duration line, e.g. "✻ Cooked for 8s" / "Churned for 5s". Anchored
# end-to-end so a real sentence ("ran for 30s and passed") is NOT stripped.
_DURATION_RE = re.compile(r"^\W*\w+ for \d+s\W*$", re.IGNORECASE)
# Only glyphs / box-drawing / punctuation (a bare "›" prompt, a "─────" rule).
_GLYPHS_ONLY_RE = re.compile(r"^[\W_]+$")
# A leading prompt marker on an otherwise-real line ("› make it a haiku"), peeled
# for a clean summary. Only a small known set, each followed by whitespace.
_LEAD_PROMPT_RE = re.compile(r"^(?:[›❯»>$#%•·*]+|►+)\s+")


@dataclass
class Summary:
    """One pane's summary plus whether it appears to await input."""
    text: str
    needs_input: bool
    source: str  # "llm" when the summarizer produced it, "fallback" otherwise


def build_prompt(tail: str) -> str:
    """The single argv argument: instruction followed by the denoised tail."""
    return f"{_INSTRUCTION}\n\n--- recent agent output ---\n{denoise(tail)}"


def _clean(text: str) -> str:
    """Strip ANSI escapes and collapse whitespace to a single line."""
    return " ".join(_ANSI_RE.sub("", text).split())


def _is_chrome(line: str) -> bool:
    """Whether a cleaned line is TUI chrome rather than work content."""
    low = line.lower()
    if any(s in low for s in _CHROME_SUBSTRINGS):
        return True
    return bool(_DURATION_RE.match(line) or _GLYPHS_ONLY_RE.match(line))


def _content_lines(tail: str) -> list[str]:
    """Cleaned, chrome-free, non-empty lines of `tail`, in order.

    A leading prompt glyph on a real line is peeled ("› make it a haiku" ->
    "make it a haiku") so a summary/fallback reads cleanly.
    """
    out: list[str] = []
    for ln in tail.splitlines():
        c = _clean(ln)
        if not c or _is_chrome(c):
            continue
        c = _LEAD_PROMPT_RE.sub("", c).strip()
        if c:
            out.append(c)
    return out


def denoise(tail: str) -> str:
    """Drop TUI chrome and blank lines so the summarizer sees work, not UI."""
    return "\n".join(_content_lines(tail))


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
    """Non-LLM summary: the last meaningful (chrome-free) line of scrollback."""
    needs = detect_needs_input(tail)
    lines = _content_lines(tail)
    if not lines:
        return Summary("(no output)", needs, "fallback")
    return Summary(lines[-1][:_FALLBACK_MAX], needs, "fallback")


def summarize(tail: str, *, cmd: str, timeout: float = 20.0,
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
