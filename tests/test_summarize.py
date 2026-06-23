import subprocess

from vupai.summarize import Summary, build_prompt, summarize


def _runner(stdout="", returncode=0, *, capture=None):
    """Fake subprocess.run: records argv into `capture`, returns canned output."""
    def run(argv, **kwargs):
        if capture is not None:
            capture.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")
    return run


def test_clean_single_line_is_used_verbatim():
    s = summarize("scrollback", cmd="claude -p",
                  runner=_runner("Refactored auth, tests green."))
    assert s.text == "Refactored auth, tests green."
    assert s.source == "llm"
    assert s.needs_input is False


def test_extracts_last_nonblank_line_from_interleaved_stdout():
    """codex/ollama-style: trace lines first, the answer last."""
    noisy = "[event] thinking\n[event] tool_call read\n\nDone: added retries.\n"
    s = summarize("x", cmd="codex exec", runner=_runner(noisy))
    assert s.text == "Done: added retries."
    assert s.source == "llm"


def test_needs_prefix_sets_flag_and_is_stripped():
    s = summarize("x", cmd="claude -p", runner=_runner("NEEDS: approve migration?"))
    assert s.needs_input is True
    assert s.text == "approve migration?"


def test_ansi_is_stripped():
    s = summarize("x", cmd="claude -p", runner=_runner("\x1b[32mall green\x1b[0m"))
    assert s.text == "all green"


def test_trailing_ansi_only_line_does_not_mask_summary():
    # codex/gemini/ollama may print a cursor-show / color-reset line last.
    out = "Refactored the parser.\n\x1b[0m\x1b[?25h"
    s = summarize("x", cmd="codex exec", runner=_runner(out))
    assert s.source == "llm"
    assert s.text == "Refactored the parser."


def test_needs_signal_survives_trailing_ansi_line():
    out = "NEEDS: should I deploy?\n\x1b[0m"
    s = summarize("x", cmd="claude -p", runner=_runner(out))
    assert s.needs_input is True
    assert s.text == "should I deploy?"


def test_nonzero_exit_falls_back_to_last_meaningful_line():
    s = summarize("line one\nbuild failed: boom\n", cmd="claude -p",
                  runner=_runner("", returncode=1))
    assert s.source == "fallback"
    assert s.text == "build failed: boom"


def test_missing_command_falls_back():
    def boom(argv, **kwargs):
        raise FileNotFoundError(argv[0])
    s = summarize("only line here", cmd="nonexistent-tool", runner=boom)
    assert s.source == "fallback"
    assert s.text == "only line here"


def test_timeout_falls_back():
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 12.0)
    s = summarize("a\nb\nlast", cmd="claude -p", runner=slow)
    assert s.source == "fallback"
    assert s.text == "last"


def test_empty_stdout_falls_back_but_keeps_needs_signal():
    s = summarize("Continue? [y/n]", cmd="claude -p", runner=_runner("   \n  \n"))
    assert s.source == "fallback"
    assert s.needs_input is True


def test_blank_command_falls_back():
    s = summarize("something", cmd="   ", runner=_runner("ignored"))
    assert s.source == "fallback"


def test_max_chars_truncates():
    long = "x" * 200
    s = summarize("t", cmd="claude -p", max_chars=20, runner=_runner(long))
    assert len(s.text) == 20


def test_non_claude_command_pipeline_has_no_claude_coupling():
    """Swap in an arbitrary tool: argv is built from the cmd string and the
    prompt carries the tail. Nothing in the path assumes Claude."""
    seen: list = []
    s = summarize("PANE TAIL TEXT", cmd="mytool run --flag",
                  runner=_runner("ok summary", capture=seen))
    assert s.text == "ok summary"
    argv = seen[0]
    assert argv[:3] == ["mytool", "run", "--flag"]
    assert "PANE TAIL TEXT" in argv[-1]          # tail rides in the prompt arg
    assert len(argv) == 4                          # exactly cmd tokens + 1 prompt


def test_build_prompt_contains_instruction_and_tail():
    p = build_prompt("THE TAIL")
    assert "THE TAIL" in p
    assert "ONE line" in p


def test_fallback_no_output():
    s = summarize("", cmd="claude -p", runner=_runner(""))
    assert s == Summary("(no output)", False, "fallback")
