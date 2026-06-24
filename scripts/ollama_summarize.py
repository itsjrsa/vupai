#!/usr/bin/env python3
"""Ollama summarizer adapter for vupai's board_summarizer_cmd.

vupai runs `board_summarizer_cmd` with the prompt as the FINAL argv argument and
reads stdout (the board keeps the last non-blank line; `read` keeps the whole
reply). This adapter forwards that prompt to an Ollama server's /api/generate and
prints the model's response, so the model can live on another machine instead of
cold-starting a CLI per call.

Config (point host/model at your box; the prompt still rides last):

    board_summarizer_cmd = \
      "python3 /abs/path/scripts/ollama_summarize.py --host http://BOX:11434 --model qwen2.5:3b"

Host/model also read from OLLAMA_HOST / OLLAMA_MODEL when the flags are absent.

Failure is silent by contract: any error prints nothing and exits non-zero, so
vupai degrades to its stdlib last-line fallback instead of speaking garbage.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"
# Warm calls are 1-4s; the FIRST call after the model is evicted pays a load.
# keep_alive=-1 (below) keeps it resident, but allow headroom for that load.
DEFAULT_TIMEOUT = 30.0
# Cap generation so a runaway model can't blow the timeout. The board wants one
# line, `read` wants 2-4 sentences; 256 tokens covers both.
NUM_PREDICT = 256

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _parse_argv(argv: list[str]) -> tuple[str, str, float, str] | None:
    """Pull --host/--model/--timeout flags; the final bare token is the prompt."""
    host = os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
    model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    timeout = float(os.environ.get("OLLAMA_TIMEOUT", DEFAULT_TIMEOUT))
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--host", "--model", "--timeout") and i + 1 < len(argv):
            val = argv[i + 1]
            if a == "--host":
                host = val
            elif a == "--model":
                model = val
            else:
                timeout = float(val)
            i += 2
            continue
        rest.append(a)
        i += 1
    if not rest:
        return None
    # The prompt is the last positional arg vupai appended.
    return host.rstrip("/"), model, timeout, rest[-1]


class ThinkStripper:
    """Drop <think>...</think> spans from a token stream, incrementally.

    Reasoning models (e.g. qwen3) emit a chain-of-thought block vupai must never
    speak. `feed` returns only the text that is safe to emit now: complete think
    blocks are removed, an unclosed <think> (and everything after it) is held back
    until it closes, and a trailing partial tag ("<", "<thi", "</") is held until
    the next token disambiguates it. The emitted prefix is stable - future
    removals only ever affect text after it - so emitting len-deltas is correct.
    """

    def __init__(self):
        self._buf = ""
        self._emitted = 0

    @staticmethod
    def _could_be_tag(s: str) -> bool:
        low = s.lower()
        return "<think>".startswith(low) or "</think>".startswith(low)

    def _safe_prefix(self, *, final: bool) -> str:
        cleaned = _THINK_RE.sub("", self._buf)
        cut = cleaned.lower().find("<think>")  # unclosed open: hold it + the rest
        if cut != -1:
            cleaned = cleaned[:cut]
        elif not final:
            lt = cleaned.rfind("<")  # hold a possible partial tag at the tail
            if lt != -1 and self._could_be_tag(cleaned[lt:]):
                cleaned = cleaned[:lt]
        return cleaned

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        cleaned = self._safe_prefix(final=False)
        out = cleaned[self._emitted:]
        self._emitted = len(cleaned)
        return out

    def flush(self) -> str:
        cleaned = self._safe_prefix(final=True)
        out = cleaned[self._emitted:]
        self._emitted = len(cleaned)
        return out


def _generate(host: str, model: str, prompt: str, timeout: float, *, write) -> bool:
    """Stream a completion, relaying think-free text to `write`. Returns whether
    any text was emitted (False -> caller exits non-zero so vupai falls back)."""
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": True,
        # Keep the model resident so the next edge-triggered summary skips reload.
        "keep_alive": -1,
        "options": {"temperature": 0.2, "num_predict": NUM_PREDICT},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    stripper = ThinkStripper()
    state = {"started": False}

    def _emit(text: str) -> None:
        if not text:
            return
        if not state["started"]:
            text = text.lstrip()  # trim leading whitespace; all-blank -> nothing
            if not text:
                return
            state["started"] = True
        write(text)

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:  # one JSON object per line (stream:true)
            raw = raw.strip()
            if not raw:
                continue
            chunk = json.loads(raw).get("response", "")
            if chunk:
                _emit(stripper.feed(chunk))
    _emit(stripper.flush())
    return state["started"]


def main(argv: list[str]) -> int:
    parsed = _parse_argv(argv)
    if parsed is None:
        return 1  # no prompt -> let vupai fall back
    host, model, timeout, prompt = parsed

    def _write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()  # stream: surface each token instead of buffering

    try:
        emitted = _generate(host, model, prompt, timeout, write=_write)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        # Unreachable host, timeout, bad JSON: stay silent, exit non-zero.
        return 1
    if not emitted:
        return 1
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
