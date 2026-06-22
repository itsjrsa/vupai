import shutil
import subprocess
import uuid

import pytest

from vupai import injector


class FakeIO:
    """In-memory stand-in for vupai.tmuxio. capture_pane returns queued frames."""

    def __init__(self, capture_frames: list[str]) -> None:
        self._frames = list(capture_frames)
        self.loaded: list[str] = []
        self.pasted: list[str] = []
        self.entered: list[str] = []

    def load_buffer(self, text: str) -> None:
        self.loaded.append(text)

    def paste_buffer(self, pane_id: str) -> None:
        self.pasted.append(pane_id)

    def capture_pane(self, pane_id: str) -> str:
        # Return the next frame; repeat the last frame once exhausted.
        if len(self._frames) > 1:
            return self._frames.pop(0)
        return self._frames[0] if self._frames else ""

    def send_enter(self, pane_id: str) -> None:
        self.entered.append(pane_id)


def test_inject_success_when_needle_present_immediately() -> None:
    text = "run the tests"
    io = FakeIO(capture_frames=[f"$ {text}"])

    result = injector.inject(
        "%3", text, confirm_timeout=0.2, poll_interval=0.01, io=io
    )

    assert result is True
    assert io.loaded == [text]
    assert io.pasted == ["%3"]
    assert io.entered == ["%3"]  # exactly one Enter


def test_inject_timeout_retries_once_and_never_sends_enter() -> None:
    text = "deploy now"
    # capture_pane never contains the needle -> both attempts time out.
    io = FakeIO(capture_frames=["$ unrelated output"])

    result = injector.inject(
        "%7", text, confirm_timeout=0.05, poll_interval=0.01, io=io
    )

    assert result is False
    assert io.pasted == ["%7", "%7"]   # initial paste + exactly one retry
    assert io.loaded == [text, text]   # load_buffer called per paste attempt
    assert io.entered == []            # Enter NEVER sent on failure


def test_inject_succeeds_when_needle_appears_on_third_poll() -> None:
    text = "git status"
    # Frames 1-2 lack the needle; frame 3 contains it. No retry should occur.
    io = FakeIO(
        capture_frames=[
            "$ ",
            "$ gi",
            f"$ {text}",
        ]
    )

    result = injector.inject(
        "%2", text, confirm_timeout=1.0, poll_interval=0.001, io=io
    )

    assert result is True
    assert io.pasted == ["%2"]   # only the initial paste, no retry
    assert io.loaded == [text]
    assert io.entered == ["%2"]  # exactly one Enter


def test_needle_uses_trailing_40_chars_of_last_line() -> None:
    long_last = "x" * 100
    text = f"first line\n{long_last}"
    needle = long_last[-40:]
    # Pane shows only the trailing 40 chars (e.g. wrapped/scrolled) -> still confirms.
    io = FakeIO(capture_frames=[f"prompt {needle}"])

    result = injector.inject(
        "%9", text, confirm_timeout=0.2, poll_interval=0.01, io=io
    )

    assert result is True
    assert io.entered == ["%9"]


def test_needle_ignores_trailing_newline_no_spurious_enter() -> None:
    # "submit\n" -> needle must NOT be ""; pane never shows "submit" -> must return False.
    text = "submit\n"
    io = FakeIO(capture_frames=["unrelated"])

    result = injector.inject(
        "%1", text, confirm_timeout=0.05, poll_interval=0.01, io=io
    )

    assert result is False
    assert io.entered == []  # Enter must never fire without confirmation


def test_needle_matches_last_nonempty_line_with_trailing_newline() -> None:
    # "submit\n" -> needle is "submit"; pane shows "submit" -> must return True.
    text = "submit\n"
    io = FakeIO(capture_frames=["submit"])

    result = injector.inject(
        "%1", text, confirm_timeout=0.2, poll_interval=0.01, io=io
    )

    assert result is True
    assert io.entered == ["%1"]  # exactly one Enter sent


def test_inject_confirms_when_needle_straddles_a_wrap() -> None:
    # A long single-line utterance word-wraps across rows in the target pane, so
    # the trailing-40 needle is split over two captured lines with indentation.
    # Whitespace-insensitive matching must still confirm it (regression: long
    # utterances failed with "text not confirmed").
    text = (
        "everything on macOS and I don't have a Linux machine "
        "right now with me but do you think this will work?"
    )
    wrapped_capture = (
        "> everything on macOS and I don't have a Linux machine right now with me but\n"
        "  do you think this will work?"
    )
    io = FakeIO(capture_frames=[wrapped_capture])

    result = injector.inject(
        "%5", text, confirm_timeout=0.2, poll_interval=0.01, io=io
    )

    assert result is True
    assert io.entered == ["%5"]  # exactly one Enter, no retry-paste duplication
    assert io.pasted == ["%5"]


class _LateLandIO:
    """capture_pane returns absent on the first call (the initial attempt's poll)
    and present thereafter (the first paste landed late, during the retry)."""

    def __init__(self, needle_text: str) -> None:
        self._needle_text = needle_text
        self.captures = 0
        self.loaded: list[str] = []
        self.pasted: list[str] = []
        self.entered: list[str] = []

    def load_buffer(self, text: str) -> None:
        self.loaded.append(text)

    def paste_buffer(self, pane_id: str) -> None:
        self.pasted.append(pane_id)

    def capture_pane(self, pane_id: str) -> str:
        self.captures += 1
        return "" if self.captures == 1 else f"$ {self._needle_text}"

    def send_enter(self, pane_id: str) -> None:
        self.entered.append(pane_id)


def test_retry_does_not_double_paste_when_first_paste_lands_late() -> None:
    # First attempt times out, but the paste lands just after. The retry must
    # detect the text is already present and Enter WITHOUT pasting again -
    # otherwise the text is doubled and the agent receives it twice.
    text = "deploy the build"
    io = _LateLandIO(text)

    result = injector.inject(
        "%4", text, confirm_timeout=0.0, poll_interval=0.001, io=io
    )

    assert result is True
    assert io.pasted == ["%4"]   # exactly ONE paste; the retry skipped re-pasting
    assert io.loaded == [text]
    assert io.entered == ["%4"]  # exactly one Enter


def test_submit_delay_sends_enter_when_text_still_present() -> None:
    # With a review delay, the text is still in the pane after the pause -> Enter.
    text = "run the tests"
    io = FakeIO(capture_frames=[f"$ {text}"])

    result = injector.inject(
        "%1", text, confirm_timeout=0.2, poll_interval=0.01,
        submit_delay=0.001, io=io)

    assert result is True
    assert io.entered == ["%1"]


def test_submit_delay_cancels_when_input_cleared() -> None:
    # The paste is confirmed, but during the review window the user clears the
    # input (needle gone on the post-delay re-check) -> cancel: no Enter, None.
    text = "delete everything"
    io = FakeIO(capture_frames=[f"$ {text}", "$ "])  # present, then cleared

    result = injector.inject(
        "%2", text, confirm_timeout=0.2, poll_interval=0.01,
        submit_delay=0.001, io=io)

    assert result is None            # cancelled (distinct from False)
    assert io.entered == []          # Enter NEVER sent
    assert io.pasted == ["%2"]       # pasted once, not re-pasted


def test_submit_delay_zero_sends_enter_immediately() -> None:
    text = "hello world"
    io = FakeIO(capture_frames=[f"$ {text}"])

    result = injector.inject(
        "%1", text, confirm_timeout=0.2, poll_interval=0.01,
        submit_delay=0.0, io=io)

    assert result is True
    assert io.entered == ["%1"]


@pytest.mark.integration
def test_inject_delivers_line_to_real_cat_pane() -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")

    from vupai import tmuxio  # real module, no fake io

    session = f"vupai-it-{uuid.uuid4().hex[:8]}"
    # `cat` echoes each submitted line back to the pane, proving the Enter landed.
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "80", "-y", "24", "cat"],
        check=True,
    )
    try:
        pane_id = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        line = f"hello-{uuid.uuid4().hex[:6]}"
        ok = injector.inject(
            pane_id, line, confirm_timeout=3.0, poll_interval=0.05, io=tmuxio
        )
        assert ok is True

        # cat echoes the line; it must appear at least twice (typed + echoed).
        captured = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id],
            check=True, capture_output=True, text=True,
        ).stdout
        assert captured.count(line) >= 2
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False)
