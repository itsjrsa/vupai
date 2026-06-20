from dataclasses import dataclass

from voxpane.feedback import Feedback
from voxpane.router import Route


@dataclass
class FakeIO:
    """Records display_message + set_status calls so tests can assert on them."""
    calls: list[tuple[str, str]]
    status: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.status is None:
            self.status = []

    def display_message(self, pane_id: str, message: str) -> None:
        self.calls.append((pane_id, message))

    def set_status(self, text: str) -> None:
        self.status.append(text)


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


def test_announce_sets_ok_indicator_with_name():
    io = FakeIO(calls=[])
    fb = Feedback(io=io)
    fb.announce(make_route(pane_id="%3", text="go", matched_name="nova", fallback=False))
    assert len(io.status) == 1
    assert "nova" in io.status[0] and "▸" in io.status[0]


def test_error_sets_error_indicator():
    io = FakeIO(calls=[])
    fb = Feedback(io=io)
    fb.error("no target")
    assert io.status and "no target" in io.status[0] and "⚠" in io.status[0]


def test_listening_and_working_and_ready_set_indicator():
    io = FakeIO(calls=[])
    fb = Feedback(io=io)
    fb.listening("system")
    fb.working()
    fb.ready()
    assert any("listening" in s and "◉" in s for s in io.status)
    assert any("working" in s for s in io.status)
    assert any("voxpane" in s and "●" in s for s in io.status)


def test_indicator_disabled_skips_set_status():
    io = FakeIO(calls=[])
    fb = Feedback(io=io, indicator_enabled=False)
    fb.error("boom")
    fb.working()
    assert io.status == []


def test_indicator_truncates_long_label():
    io = FakeIO(calls=[])
    fb = Feedback(io=io)
    fb.error("x" * 200)
    # label is capped; the styled wrapper adds the glyph + style codes around it
    assert io.status[0].count("x") == 36


def test_stale_indicator_write_does_not_clobber_newer_state():
    # Simulates the quick-tap race: a 'listening' write is reserved at press time
    # but lands AFTER a later working/result write. It must be dropped.
    io = FakeIO(calls=[])
    fb = Feedback(io=io)
    listen_seq = fb.reserve()        # press: reserve now, write later
    fb.working()                     # later state, written first
    fb.listening("system", listen_seq)  # the delayed press-time write arrives
    assert "working" in io.status[-1]    # newer state survived
    assert "listening" not in io.status[-1]


def test_in_order_writes_all_apply():
    io = FakeIO(calls=[])
    fb = Feedback(io=io)
    fb.working()
    fb.error("boom")
    assert "working" in io.status[0]
    assert "boom" in io.status[1]


def test_indicator_swallows_io_without_set_status():
    # An io fake lacking set_status must not break the pipeline.
    class Bare:
        def display_message(self, *_): ...
    Feedback(io=Bare()).error("boom")  # no raise
