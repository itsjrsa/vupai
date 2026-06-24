#!/usr/bin/env python3
"""Streaming claude summarizer for vupai's board_summarizer_cmd (the default).

Plain `claude -p` buffers: it prints the whole reply only when generation
finishes, so vupai's streaming "read" has nothing to speak until the end. This
runs claude in stream-json mode and relays the assistant's text deltas to stdout
AS THEY ARRIVE, so the read command can speak sentence-by-sentence (first words
out in ~1-2s, after claude's CLI cold-start). Thinking deltas are dropped (never
spoken). For the board it is identical to plain claude: the last non-blank line
is still the summary, it just streamed there.

It is the default summarizer, invoked as `python -m vupai.claude_summarize
--model claude-haiku-4-5` (the prompt rides last); --model also reads from
$CLAUDE_MODEL. Failure is silent + non-zero (nothing relayed) so vupai degrades
to its stdlib fallback. Swap board_summarizer_cmd for plain `claude -p`,
`ollama_summarize.py`, etc. to opt out.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

DEFAULT_MODEL = "claude-haiku-4-5"


def _parse_argv(argv: list[str]) -> tuple[str, str] | None:
    """Pull --model (else $CLAUDE_MODEL); the final bare token is the prompt."""
    model = os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--model" and i + 1 < len(argv):
            model = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    if not rest:
        return None
    return model, rest[-1]


def _text_delta(line: str) -> str | None:
    """Extract assistant text from one stream-json line, or None.

    Shape: {"type":"stream_event","event":{"type":"content_block_delta",
    "delta":{"type":"text_delta","text":"..."}}}. Thinking deltas and every
    other event type yield None.
    """
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    if obj.get("type") != "stream_event":
        return None
    event = obj.get("event") or {}
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    if delta.get("type") != "text_delta":
        return None
    return delta.get("text")


def main(argv: list[str]) -> int:
    parsed = _parse_argv(argv)
    if parsed is None:
        return 1
    model, prompt = parsed
    cmd = [
        "claude", "-p", "--model", model,
        "--output-format", "stream-json", "--include-partial-messages",
        "--verbose", prompt,
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except (FileNotFoundError, OSError):
        return 1
    relayed = False
    try:
        for line in proc.stdout:
            text = _text_delta(line)
            if text:
                sys.stdout.write(text)
                sys.stdout.flush()  # stream: don't let the OS buffer hold it back
                relayed = True
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        proc.wait()
    if relayed:
        sys.stdout.write("\n")
    return 0 if relayed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
