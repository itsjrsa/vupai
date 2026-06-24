"""Hermetic tests for the Ollama summarizer adapter (no network, no Ollama).

The adapter is the optional bridge from vupai's `board_summarizer_cmd` contract
to a (remote) Ollama server. These lock in the contract that matters to vupai:
prompt rides as the final argv arg, host/model come from flags or env, the
request is a well-formed stream, reasoning scratchpads are stripped from the
streamed tokens, and every failure is silent + non-zero so the summarizer
degrades to its stdlib fallback.
"""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ollama_summarize",
    Path(__file__).resolve().parent.parent / "scripts" / "ollama_summarize.py",
)
osum = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(osum)


def _stream_urlopen(captured, *response_chunks):
    """A urlopen stub that records the request and streams JSONL `response` lines."""
    def _open(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode())
        lines = b"".join(
            (json.dumps({"response": c, "done": False}) + "\n").encode()
            for c in response_chunks
        )
        return io.BytesIO(lines)
    return _open


# --- argv / env parsing -----------------------------------------------------

def test_parse_argv_flags_win_and_prompt_is_last():
    host, model, timeout, prompt = osum._parse_argv(
        ["--host", "http://box:11434", "--model", "qwen2.5:3b", "--timeout", "12", "the prompt"]
    )
    assert host == "http://box:11434"
    assert model == "qwen2.5:3b"
    assert timeout == 12.0
    assert prompt == "the prompt"


def test_parse_argv_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://envbox:11434/")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    host, model, _timeout, prompt = osum._parse_argv(["only the prompt"])
    assert host == "http://envbox:11434"
    assert model == "llama3.2:3b"
    assert prompt == "only the prompt"


def test_parse_argv_no_prompt_is_none():
    assert osum._parse_argv(["--host", "http://box:11434"]) is None


# --- streaming request + relay ----------------------------------------------

def test_generate_streams_request_and_relays_tokens(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        osum.urllib.request, "urlopen",
        _stream_urlopen(captured, "The agent ", "fixed ", "the bug."),
    )
    out = []
    emitted = osum._generate("http://box:11434", "qwen2.5:3b", "summarize", 30.0,
                             write=out.append)
    assert emitted is True
    assert "".join(out) == "The agent fixed the bug."
    assert captured["url"] == "http://box:11434/api/generate"
    body = captured["body"]
    assert body["model"] == "qwen2.5:3b"
    assert body["prompt"] == "summarize"
    assert body["stream"] is True  # streamed so the read command can speak live
    assert body["keep_alive"] == -1  # keeps the remote model resident


def test_generate_strips_think_block_across_chunks(monkeypatch):
    # A reasoning model's scratchpad, split across stream chunks, must not leak.
    monkeypatch.setattr(
        osum.urllib.request, "urlopen",
        _stream_urlopen({}, "<thi", "nk>secret ", "reasoning</thi", "nk>The answer."),
    )
    out = []
    osum._generate("http://box:11434", "qwen3:1.7b", "p", 30.0, write=out.append)
    assert "".join(out) == "The answer."


def test_main_prints_stream_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        osum.urllib.request, "urlopen", _stream_urlopen({}, "A one ", "line summary."),
    )
    rc = osum.main(["--host", "http://box:11434", "--model", "qwen2.5:3b", "a prompt"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "A one line summary."


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("unreachable"),
    OSError("timed out"),
    ValueError("bad json"),
])
def test_main_fails_silently_nonzero(monkeypatch, capsys, exc):
    def _boom(req, timeout=None):
        raise exc
    monkeypatch.setattr(osum.urllib.request, "urlopen", _boom)
    rc = osum.main(["--host", "http://box:11434", "x"])
    assert rc == 1
    assert capsys.readouterr().out == ""  # silent so vupai uses its fallback


def test_main_empty_stream_is_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(osum.urllib.request, "urlopen", _stream_urlopen({}, "   "))
    rc = osum.main(["--host", "http://box:11434", "x"])
    assert rc == 1
    assert capsys.readouterr().out == ""


# --- ThinkStripper ----------------------------------------------------------

def test_think_stripper_passes_plain_text_through():
    s = osum.ThinkStripper()
    assert s.feed("hello ") == "hello "
    assert s.feed("world.") == "world."
    assert s.flush() == ""


def test_think_stripper_holds_back_partial_tag_then_emits_literal():
    s = osum.ThinkStripper()
    # "< " can't be a tag prefix, so it streams through as a literal.
    assert s.feed("a < ") == "a < "
    s2 = osum.ThinkStripper()
    assert s2.feed("done <") == "done "  # trailing "<" could open <think>, held
    assert s2.flush() == "<"  # at EOF the lone "<" is literal


def test_think_stripper_drops_complete_block_and_keeps_answer():
    s = osum.ThinkStripper()
    out = s.feed("<think>private</think>Public answer.")
    assert out == "Public answer."
