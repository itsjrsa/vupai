"""Hermetic tests for the Ollama summarizer adapter (no network, no Ollama).

The adapter is the optional bridge from vupai's `board_summarizer_cmd` contract
to a (remote) Ollama server. These lock in the contract that matters to vupai:
prompt rides as the final argv arg, host/model come from flags or env, the
request is well-formed, reasoning scratchpads are stripped, and every failure is
silent + non-zero so the summarizer degrades to its stdlib fallback.
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


def _fake_urlopen(captured, response_text):
    """A urlopen stub that records the request and returns a canned response."""
    def _open(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode())
        return io.BytesIO(json.dumps({"response": response_text}).encode())
    return _open


def test_parse_argv_flags_win_and_prompt_is_last():
    host, model, timeout, prompt = osum._parse_argv(
        ["--host", "http://box:11434", "--model", "qwen2.5:3b", "--timeout", "12", "the prompt"]
    )
    assert host == "http://box:11434"  # trailing slash stripped when present
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


def test_generate_builds_request_and_strips_think(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        osum.urllib.request, "urlopen",
        _fake_urlopen(captured, "<think>scratch</think>The agent fixed the bug."),
    )
    out = osum._generate("http://box:11434", "qwen2.5:3b", "summarize this", 30.0)
    assert out == "The agent fixed the bug."  # reasoning block removed
    assert captured["url"] == "http://box:11434/api/generate"
    body = captured["body"]
    assert body["model"] == "qwen2.5:3b"
    assert body["prompt"] == "summarize this"
    assert body["stream"] is False
    assert body["keep_alive"] == -1  # keeps the remote model resident


def test_main_prints_response_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        osum.urllib.request, "urlopen", _fake_urlopen({}, "A one line summary."),
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


def test_main_empty_response_is_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(osum.urllib.request, "urlopen", _fake_urlopen({}, "   "))
    rc = osum.main(["--host", "http://box:11434", "x"])
    assert rc == 1
    assert capsys.readouterr().out == ""
