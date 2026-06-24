import subprocess

from vupai.summarize import (
    Summary,
    build_prompt,
    denoise,
    summarize,
    summarize_read,
)

# A realistic idle Claude Code pane tail: a poem, a duration line, the input box
# with a queued follow-up, and the footer chrome at the very bottom.
CLAUDE_TAIL = (
    "till the terminal hushes and waits for the end.\n"
    "\n"
    "✻ Churned for 5s\n"
    "─────\n"
    "› make it a haiku\n"
    "►► auto mode on (shift+tab to cycle) · ← for agents /rc"
)


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
    assert "ONE short line" in p


def test_denoise_strips_chrome_keeps_work():
    out = denoise(CLAUDE_TAIL)
    # work and the pending request survive
    assert "waits for the end." in out
    assert "make it a haiku" in out
    # chrome is gone
    assert "auto mode on" not in out
    assert "shift+tab" not in out
    assert "Churned for 5s" not in out
    assert "─────" not in out


def test_build_prompt_excludes_footer_chrome():
    p = build_prompt(CLAUDE_TAIL)
    assert "auto mode on" not in p
    assert "make it a haiku" in p


def test_fallback_skips_chrome_and_picks_real_line():
    # Summarizer fails -> fallback must not echo the footer; it picks the last
    # real line (the queued request), not "auto mode on ...".
    s = summarize(CLAUDE_TAIL, cmd="claude -p", runner=_runner("", returncode=1))
    assert s.source == "fallback"
    assert "auto mode on" not in s.text
    assert s.text == "make it a haiku"


def test_fallback_no_output():
    s = summarize("", cmd="claude -p", runner=_runner(""))
    assert s == Summary("(no output)", False, "fallback")


# --- summarize_read: the richer, spoken read-back summary --------------------


def test_summarize_read_keeps_whole_reply_and_embeds_title():
    cap = []
    s = summarize_read(
        "agent output", cmd="claude -p", title="Fix the parser",
        runner=_runner("First sentence here. Second sentence too.", capture=cap))
    assert s.source == "llm"
    # The WHOLE reply, not just the last line (that's the board's job).
    assert s.text == "First sentence here. Second sentence too."
    # The pane title rode into the prompt (the single argv arg).
    assert "Fix the parser" in cap[0][-1]


def test_summarize_read_collapses_multiline_reply_to_one_paragraph():
    s = summarize_read("x", cmd="claude -p",
                       runner=_runner("Line one.\n\nLine two.\n"))
    assert s.text == "Line one. Line two."


def test_summarize_read_truncates_on_a_sentence_boundary():
    reply = "One. " * 80  # many sentences, ~400 chars
    s = summarize_read("x", cmd="claude -p", max_chars=50,
                       runner=_runner(reply.strip()))
    assert len(s.text) <= 50
    assert s.text.endswith(".")  # a complete sentence, never mid-word


def test_summarize_read_falls_back_on_failure():
    s = summarize_read("a\nbuild failed: boom\n", cmd="claude -p",
                       runner=_runner("", returncode=1))
    assert s.source == "fallback"
    assert s.text == "build failed: boom"


def test_summarize_read_falls_back_on_empty_command():
    s = summarize_read("a\nlast meaningful line", cmd="",
                       runner=_runner("ignored"))
    assert s.source == "fallback"
    assert s.text == "last meaningful line"
