from voxpane.commands import Command, execute_command, handle_command, parse_command
from voxpane.config import Config
from voxpane.registry import Pane


class FakeTmux:
    def __init__(self, new_ids=()):
        self.calls = []
        self._ids = list(new_ids)

    def split_window(self, target, program):
        self.calls.append(("split_window", target, program))
        return self._ids.pop(0)

    def select_layout(self, target, layout):
        self.calls.append(("select_layout", target, layout))

    def set_pane_name(self, pane_id, name):
        self.calls.append(("set_pane_name", pane_id, name))

    def select_pane(self, pane_id):
        self.calls.append(("select_pane", pane_id))

    def swap_pane(self, a, b):
        self.calls.append(("swap_pane", a, b))

    def kill_pane(self, pane_id):
        self.calls.append(("kill_pane", pane_id))


class FakeRegistry:
    def __init__(self, panes, focused=None):
        self.panes = panes
        self._focused = focused

    def focused(self):
        return self._focused

    def refresh(self):
        pass


def _pane(id, name, window_id="@1", active=False):
    return Pane(id=id, window_id=window_id, window="main", index=0,
                name=name, command="zsh", active=active)


def test_execute_create_splits_names_and_tiles():
    focused = _pane("%0", "%0", active=True)  # unnamed focused pane
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(new_ids=["%1", "%2"])
    cmd = Command(kind="create", count=2, program=None, unit="pane")
    res = execute_command(cmd, reg, Config(), io=io)
    assert res.ok
    assert ("split_window", "@1", "claude") in io.calls           # default program
    assert ("set_pane_name", "%1", "nova") in io.calls
    assert ("set_pane_name", "%2", "atlas") in io.calls
    assert io.calls[-1] == ("select_layout", "@1", "tiled")


def test_execute_create_windows_not_supported():
    focused = _pane("%0", "%0", active=True)
    reg = FakeRegistry([focused], focused=focused)
    res = execute_command(Command(kind="create", count=2, unit="window"),
                          reg, Config(), io=FakeTmux())
    assert res.ok is False and "window" in res.message


def test_execute_unknown_does_not_touch_tmux():
    io = FakeTmux()
    res = execute_command(Command(kind="unknown", raw="blah"), FakeRegistry([]),
                          Config(), io=io)
    assert res.ok is False and io.calls == []


def test_handle_command_none_when_not_addressed():
    assert handle_command("frontend run tests", FakeRegistry([]), Config()) is None


def _parse(text, cfg=None):
    cfg = cfg or Config()
    return parse_command(
        text, control_word=cfg.control_word, broadcast_word=cfg.broadcast_word,
        macros=cfg.macros, programs=cfg.programs)


def test_parse_not_addressed_returns_none():
    assert _parse("frontend run the tests") is None


def test_parse_create_default_program():
    c = _parse("computer, create four panes")
    assert c.kind == "create" and c.count == 4 and c.program is None and c.unit == "pane"


def test_parse_create_explicit_shell_program():
    c = _parse("computer create two shell panes")
    assert c.kind == "create" and c.count == 2 and c.program == ""


def test_parse_create_windows_unit():
    c = _parse("computer make two windows")
    assert c.kind == "create" and c.count == 2 and c.unit == "window"


def test_parse_unknown_when_addressed_gibberish():
    c = _parse("computer flibbertigibbet")
    assert c.kind == "unknown" and c.raw == "flibbertigibbet"


def test_parse_create_unknown_program_is_unknown():
    c = _parse("computer create two banana panes")
    assert c.kind == "unknown"


def test_parse_control_word_configurable():
    cfg = Config()
    c = parse_command(
        "jarvis create one pane", control_word="jarvis",
        broadcast_word="team", macros={}, programs=cfg.programs)
    assert c.kind == "create" and c.count == 1


def test_parse_broadcast_preserves_text():
    c = _parse("everyone run the tests")
    assert c.kind == "broadcast" and c.text == "run the tests"


def test_parse_macro_matches_normalized_phrase():
    cfg = Config()
    object.__setattr__(cfg, "macros", {"dev layout": ["create 3 claude panes", "tile"]})
    c = _parse("computer, Dev Layout", cfg)
    assert c.kind == "macro" and c.actions == ("create 3 claude panes", "tile")


def test_execute_macro_runs_create_then_tile():
    focused = _pane("%0", "%0", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(new_ids=["%1", "%2", "%3"])
    cmd = Command(kind="macro", actions=("create 3 claude panes", "tile"))
    res = execute_command(cmd, reg, Config(), io=io)
    assert res.ok
    splits = [c for c in io.calls if c[0] == "split_window"]
    assert len(splits) == 3
    assert io.calls[-1] == ("select_layout", "@1", "tiled")


def test_execute_focus_selects_named_pane():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])
    io = FakeTmux()
    res = execute_command(Command(kind="focus", name="atlas"), reg, Config(), io=io)
    assert res.ok and io.calls == [("select_pane", "%2")]


def test_execute_focus_unknown_name():
    reg = FakeRegistry([_pane("%1", "nova", active=True)])
    res = execute_command(Command(kind="focus", name="zzzz"), reg, Config(), io=FakeTmux())
    assert res.ok is False


def test_execute_swap_two_named_panes():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])
    io = FakeTmux()
    res = execute_command(Command(kind="swap", name="nova", name_b="atlas"),
                          reg, Config(), io=io)
    assert res.ok and io.calls == [("swap_pane", "%1", "%2")]


def test_execute_swap_unknown_name():
    panes = [_pane("%1", "nova", active=True)]
    res = execute_command(Command(kind="swap", name="nova", name_b="zzzz"),
                          FakeRegistry(panes, focused=panes[0]), Config(), io=FakeTmux())
    assert res.ok is False


def test_execute_swap_unknown_first_name():
    panes = [_pane("%1", "nova", active=True)]
    res = execute_command(Command(kind="swap", name="zzzz", name_b="nova"),
                          FakeRegistry(panes, focused=panes[0]), Config(), io=FakeTmux())
    assert res.ok is False


def test_execute_swap_ambiguous_name_does_not_swap():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "novo")]
    io = FakeTmux()
    res = execute_command(Command(kind="swap", name="nov", name_b="nova"),
                          FakeRegistry(panes, focused=panes[0]), Config(), io=io)
    assert res.ok is False
    assert not any(c[0] == "swap_pane" for c in io.calls)


def test_execute_close_named_pane():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])
    io = FakeTmux()
    res = execute_command(Command(kind="close", name="atlas"), reg, Config(), io=io)
    assert res.ok and io.calls == [("kill_pane", "%2")]


def test_parse_bare_close_is_unknown():
    c = _parse("computer close")
    assert c.kind == "unknown"


def test_execute_broadcast_injects_each_named_pane():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas"),
             _pane("%3", "%3")]  # %3 unnamed -> skipped
    reg = FakeRegistry(panes, focused=panes[0])
    sent = []

    def fake_inject(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05):
        sent.append((pane_id, text))
        return True

    res = execute_command(Command(kind="broadcast", text="run the tests"),
                          reg, Config(), io=FakeTmux(), inject_fn=fake_inject)
    assert res.ok and "2/2" in res.message
    assert sent == [("%1", "run the tests"), ("%2", "run the tests")]


def test_execute_broadcast_no_named_agents():
    reg = FakeRegistry([_pane("%1", "%1", active=True)])
    res = execute_command(Command(kind="broadcast", text="hi"), reg, Config(),
                          io=FakeTmux(), inject_fn=lambda *a, **k: True)
    assert res.ok is False


def test_execute_broadcast_empty_text():
    reg = FakeRegistry([_pane("%1", "nova", active=True)])
    res = execute_command(Command(kind="broadcast", text=""), reg, Config(),
                          io=FakeTmux(), inject_fn=lambda *a, **k: True)
    assert res.ok is False
