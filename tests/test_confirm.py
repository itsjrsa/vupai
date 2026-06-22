import vupai.confirm as confirm


class FakeRun:
    """Records the argv and simulates the popup writing a result file.

    `answer` is what the popup's shell would write ('y'/'n'); None simulates the
    popup never writing (e.g. timeout with no file)."""

    def __init__(self, answer="y"):
        self.answer = answer
        self.argv = None

    def __call__(self, argv, *, result_path, **kwargs):
        self.argv = argv
        if self.answer is not None:
            with open(result_path, "w") as fh:
                fh.write(self.answer)


def test_popup_confirm_true_on_yes(tmp_path):
    run = FakeRun(answer="y")
    assert confirm.popup_confirm(
        "close nova", timeout=1.0, run=run, tmpdir=tmp_path) is True


def test_popup_confirm_false_on_no(tmp_path):
    run = FakeRun(answer="n")
    assert confirm.popup_confirm(
        "close nova", timeout=1.0, run=run, tmpdir=tmp_path) is False


def test_popup_confirm_false_when_no_result_written(tmp_path):
    # The popup closed without writing (timeout / dismissed) -> fail-safe cancel.
    run = FakeRun(answer=None)
    assert confirm.popup_confirm(
        "close nova", timeout=0.2, run=run, tmpdir=tmp_path) is False


def test_popup_confirm_false_when_runner_raises(tmp_path):
    def boom(argv, *, result_path, **kwargs):
        raise OSError("no client / old tmux")

    assert confirm.popup_confirm(
        "close nova", timeout=1.0, run=boom, tmpdir=tmp_path) is False


def test_popup_confirm_builds_display_popup_argv_with_disable_hint(tmp_path):
    run = FakeRun(answer="y")
    confirm.popup_confirm("close nova", timeout=1.0, run=run, tmpdir=tmp_path)
    argv = run.argv
    assert "display-popup" in argv
    joined = " ".join(argv)
    assert "close nova" in joined           # the action summary is shown
    assert "confirm_destructive" in joined  # the "disable in config" hint
    # the y/n affordance is presented to the user
    assert "y" in joined.lower() and "cancel" in joined.lower()
