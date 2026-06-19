from dataclasses import dataclass

from vtmux.feedback import Feedback
from vtmux.router import Route


@dataclass
class FakeIO:
    """Records display_message calls so tests can assert on them."""
    calls: list[tuple[str, str]]

    def display_message(self, pane_id: str, message: str) -> None:
        self.calls.append((pane_id, message))


def make_route(*, pane_id, text, matched_name, fallback):
    return Route(
        pane_id=pane_id,
        text=text,
        matched_name=matched_name,
        confidence=100.0 if matched_name else 0.0,
        fallback=fallback,
    )


def test_announce_with_matched_name_uses_name_label():
    io = FakeIO(calls=[])
    fb = Feedback(io=io)
    route = make_route(pane_id="%3", text="run the tests", matched_name="backend", fallback=False)

    fb.announce(route)

    assert io.calls == [("%3", "◀ backend: run the tests")]


def test_announce_fallback_uses_focus_label():
    io = FakeIO(calls=[])
    fb = Feedback(io=io)
    route = make_route(pane_id="%7", text="git status", matched_name=None, fallback=True)

    fb.announce(route)

    assert io.calls == [("%7", "◀ (focus): git status")]


def test_announce_truncates_text_to_40_chars():
    io = FakeIO(calls=[])
    fb = Feedback(io=io)
    long_text = "x" * 100
    route = make_route(pane_id="%1", text=long_text, matched_name="api", fallback=False)

    fb.announce(route)

    assert io.calls == [("%1", "◀ api: " + "x" * 40)]


def test_announce_with_no_pane_id_does_not_call_display_message():
    io = FakeIO(calls=[])
    fb = Feedback(io=io)
    route = make_route(pane_id=None, text="hello", matched_name=None, fallback=False)

    fb.announce(route)

    assert io.calls == []


def test_status_prints_text(capsys):
    fb = Feedback(io=FakeIO(calls=[]))
    fb.status("listening")
    out = capsys.readouterr().out
    assert "listening" in out


def test_error_prints_prefixed_text(capsys):
    fb = Feedback(io=FakeIO(calls=[]))
    fb.error("boom")
    out = capsys.readouterr().out
    assert "error" in out.lower()
    assert "boom" in out
