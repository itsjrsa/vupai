from dataclasses import replace

from vupai import tips
from vupai.config import Config


def _cfg(**kw):
    base = Config()
    return replace(base, **kw)


def test_button_pool_has_command_and_hint_examples():
    cfg = _cfg(addressing="button", command_hotkey="alt_r",
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


def test_keyword_mode_drops_command_examples():
    cfg = _cfg(addressing="keyword", hotkey="alt_r")
    pool = tips.build_tips(cfg)
    joined = "\n".join(pool)
    assert "create two panes" not in joined
    assert "tip: hold alt_r to talk" in joined  # uses dictation key in keyword mode


def test_every_tip_is_prefixed_and_truncated():
    cfg = _cfg(macros={"x" * 100: ["create one pane"]})
    pool = tips.build_tips(cfg)
    assert pool, "pool is never empty"
    assert all(t.startswith("tip: ") for t in pool)
    assert all(len(t) <= tips._TIP_MAX for t in pool)


def test_order_is_deterministic():
    cfg = _cfg(addressing="button")
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
    rot.tick(); rot.tick(); rot.tick()
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
