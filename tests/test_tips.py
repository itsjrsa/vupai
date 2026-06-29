from dataclasses import replace

from vupai import tips
from vupai.config import Config


def _cfg(**kw):
    base = Config()
    return replace(base, **kw)


def test_pool_has_command_and_hint_examples():
    cfg = _cfg(command_hotkey="alt_r",
               slash_commands={"clear": "/clear"}, macros={"standup": ["create two panes"]},
               programs={"claude": "claude"})
    pool = tips.build_tips(cfg)
    joined = "\n".join(pool)
    assert "tip: create two panes" in pool
    assert "tip: focus nova" in pool
    assert "tip: clear all" in joined          # from slash_commands
    assert "tip: standup" in joined            # from macros
    assert "tip: create one claude pane" in joined  # from programs
    assert "tip: hold alt_r to talk" in joined
    assert any("status_tips=false" in t for t in pool)


def test_every_tip_is_prefixed_and_truncated():
    cfg = _cfg(macros={"x" * 100: ["create one pane"]})
    pool = tips.build_tips(cfg)
    assert pool, "pool is never empty"
    assert all(t.startswith("tip: ") for t in pool)
    assert all(len(t) <= tips._TIP_MAX for t in pool)


def test_long_tip_is_not_cut_mid_word():
    # A tip too long for _TIP_MAX must end on a word boundary (with an ellipsis),
    # never sliced through the middle of a word ("...to hide" -> "...to hid").
    rendered = tips._render("set status_tips=false in config.toml to hide tips")
    assert len(rendered) <= tips._TIP_MAX
    assert not rendered.endswith("hid")
    assert rendered.endswith("…")
    # The visible body (minus prefix and ellipsis) ends on a complete word.
    body = rendered[len(tips._TIP_PREFIX):-1]
    assert not body.endswith(" ")
    assert "hid" not in body.split()[-1] or body.split()[-1] == "hide"


def test_short_tip_is_unchanged():
    assert tips._render("focus nova") == "tip: focus nova"


def test_pool_includes_ssh_example():
    cfg = _cfg()
    pool = tips.build_tips(cfg)
    assert any("ssh" in t for t in pool)


def test_pool_includes_activity_example():
    pool = tips.build_tips(_cfg())
    assert "tip: activity" in pool


def test_order_is_deterministic():
    cfg = _cfg()
    assert tips.build_tips(cfg) == tips.build_tips(cfg)


class _FakeIO:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def set_tip(self, text):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append(text)


def test_rotator_tick_cycles_and_wraps():
    io = _FakeIO()
    rot = tips.TipRotator(["a", "b"], io=io)
    rot.tick()
    rot.tick()
    rot.tick()
    assert io.sent == ["a", "b", "a"]


def test_rotator_tick_swallows_io_errors():
    rot = tips.TipRotator(["a"], io=_FakeIO(fail=True))
    rot.tick()  # must not raise


def test_rotator_empty_pool_is_noop():
    io = _FakeIO()
    rot = tips.TipRotator([], io=io)
    rot.tick()
    rot.start()  # must not start a thread on an empty pool
    assert io.sent == []
    rot.stop()


def test_rotator_stop_clears_tip():
    # A stopped daemon must not leave a stale "tip: ..." pinned in status-left.
    io = _FakeIO()
    rot = tips.TipRotator(["a", "b"], io=io)
    rot.start()
    rot.stop()
    assert io.sent[-1] == ""  # the final write blanks the tip


def test_rotator_stop_without_start_does_not_clear():
    # Never started (e.g. empty pool) -> nothing was set, so nothing to clear.
    io = _FakeIO()
    rot = tips.TipRotator([], io=io)
    rot.stop()  # must not raise
    assert io.sent == []
