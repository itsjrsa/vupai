"""Hermetic tests for the streaming claude summarizer adapter (no claude CLI).

Covers the two pure pieces that decide correctness: argv/env parsing (prompt
rides last, --model or $CLAUDE_MODEL) and stream-json event extraction (relay
text_delta, drop thinking and every other event type).
"""

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "claude_summarize",
    Path(__file__).resolve().parent.parent / "scripts" / "claude_summarize.py",
)
csum = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(csum)


def test_parse_argv_model_flag_and_prompt_last():
    model, prompt = csum._parse_argv(["--model", "claude-haiku-4-5", "the prompt"])
    assert model == "claude-haiku-4-5"
    assert prompt == "the prompt"


def test_parse_argv_env_model_default(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    model, prompt = csum._parse_argv(["only the prompt"])
    assert model == "claude-sonnet-4-6"
    assert prompt == "only the prompt"


def test_parse_argv_no_prompt_is_none():
    assert csum._parse_argv(["--model", "x"]) is None


def _stream_event(delta_type, text):
    return json.dumps({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": delta_type, "text": text},
        },
    })


def test_text_delta_extracts_assistant_text():
    assert csum._text_delta(_stream_event("text_delta", "hello")) == "hello"


def test_text_delta_skips_thinking():
    assert csum._text_delta(_stream_event("thinking_delta", "scratch")) is None


def test_text_delta_skips_other_events_and_garbage():
    assert csum._text_delta(json.dumps({"type": "system", "subtype": "init"})) is None
    assert csum._text_delta(json.dumps({"type": "stream_event",
                                        "event": {"type": "message_start"}})) is None
    assert csum._text_delta("not json at all") is None
    assert csum._text_delta("") is None
