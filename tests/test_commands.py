from voxpane.commands import Command, execute_command, handle_command, parse_command
from voxpane.config import Config
from voxpane.registry import Pane


class FakeTmux:
    def __init__(self, new_ids=(), zoomed=False):
        self.calls = []
        self._ids = list(new_ids)
        self._zoomed = zoomed

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

    def pane_zoomed(self, pane_id):
        self.calls.append(("pane_zoomed", pane_id))
        return self._zoomed

    def toggle_zoom(self, pane_id):
        self.calls.append(("toggle_zoom", pane_id))
        self._zoomed = not self._zoomed


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
        text, broadcast_word=cfg.broadcast_word,
        macros=cfg.macros, programs=cfg.programs, slash_commands=cfg.slash_commands,
        addressing="keyword")


def _parse_btn(text, cfg=None):
    cfg = cfg or Config()
    return parse_command(
        text, broadcast_word=cfg.broadcast_word,
        macros=cfg.macros, programs=cfg.programs, slash_commands=cfg.slash_commands,
        addressing="button")


def test_parse_not_addressed_returns_none():
    assert _parse("frontend run the tests") is None


def test_parse_create_default_program():
    c = _parse_btn("create four panes")
    assert c.kind == "create" and c.count == 4 and c.program is None and c.unit == "pane"


def test_parse_create_explicit_shell_program():
    c = _parse_btn("create two shell panes")
    assert c.kind == "create" and c.count == 2 and c.program == ""


def test_parse_create_windows_unit():
    c = _parse_btn("make two windows")
    assert c.kind == "create" and c.count == 2 and c.unit == "window"


def test_parse_create_unknown_program_falls_through():
    # An unrecognized program is not a command -> None (router/inject handles it).
    assert _parse_btn("create two banana panes") is None


# --- ASR homophone tolerance for the unit noun (paints/pains -> pane) ---------
# The trailing unit token is the most-misheard part of "create N panes". A
# curated alias table maps known mis-transcriptions to the canonical unit; a
# scoring approach would over-match real words (plans/lanes/planes), so the
# table stays deterministic and the precision guards below pin its boundaries.

def test_parse_create_misheard_paints_is_pane():
    # The headline bug: "create four panes" -> "create four paints".
    c = _parse_btn("create four paints")
    assert c.kind == "create" and c.count == 4 and c.program is None and c.unit == "pane"


def test_parse_create_misheard_pains_is_pane():
    c = _parse_btn("create four pains")
    assert c.kind == "create" and c.count == 4 and c.unit == "pane"


def test_parse_create_misheard_paint_singular():
    c = _parse_btn("make one paint")
    assert c.kind == "create" and c.count == 1 and c.unit == "pane"


def test_parse_create_misheard_with_program():
    # A misheard unit still composes with a valid mid-token program.
    c = _parse_btn("create two shell pains")
    assert c.kind == "create" and c.count == 2 and c.program == "" and c.unit == "pane"


def test_resolve_unit_exact_and_aliases():
    from voxpane.commands import _resolve_unit
    assert _resolve_unit("panes") == "pane"     # exact path still wins
    assert _resolve_unit("windows") == "window"
    assert _resolve_unit("paints") == "pane"     # alias
    assert _resolve_unit("pains") == "pane"


def test_resolve_unit_rejects_real_word_lookalikes():
    # Precision guard: real English words that merely rhyme must NOT be units,
    # or "create three lanes"/"create four plans" would spawn panes.
    from voxpane.commands import _resolve_unit
    for word in ("panel", "panels", "plane", "planes", "plain", "lanes", "plans"):
        assert _resolve_unit(word) is None


def test_parse_create_rhyme_falls_through():
    # End-to-end: a rhyming real word is not a unit -> not a create -> None.
    assert _parse_btn("create three lanes") is None


def test_parse_broadcast_preserves_text():
    c = _parse("everyone run the tests")
    assert c.kind == "broadcast" and c.text == "run the tests"


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


def test_parse_bare_close_falls_through():
    # "close" with no target is not a command -> None (not swallowed).
    assert _parse_btn("close") is None


def test_parse_close_the_others():
    assert _parse_btn("close the others").kind == "close_others"
    assert _parse_btn("close others").kind == "close_others"
    assert _parse_btn("kill the others").kind == "close_others"
    assert _parse_btn("close the rest").kind == "close_others"
    assert _parse_btn("close all").kind == "close_others"
    assert _parse_btn("close all panes").kind == "close_others"
    assert not _parse_btn("close the others").name


def test_execute_close_others_kills_all_but_focused():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas"),
             _pane("%3", "%3")]  # includes an unnamed pane
    reg = FakeRegistry(panes, focused=panes[0])
    io = FakeTmux()
    res = execute_command(Command(kind="close_others"), reg, Config(), io=io)
    assert res.ok
    assert io.calls == [("kill_pane", "%2"), ("kill_pane", "%3")]


def test_execute_close_others_no_others():
    focused = _pane("%1", "nova", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux()
    res = execute_command(Command(kind="close_others"), reg, Config(), io=io)
    assert res.ok is False and io.calls == []


def test_execute_close_others_no_focused():
    panes = [_pane("%1", "nova"), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=None)
    io = FakeTmux()
    res = execute_command(Command(kind="close_others"), reg, Config(), io=io)
    assert res.ok is False and io.calls == []


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


def test_execute_broadcast_partial_success():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])
    sent = []

    def fake_inject(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05):
        sent.append(pane_id)
        return pane_id == "%1"  # first confirms, second fails

    res = execute_command(Command(kind="broadcast", text="go"), reg, Config(),
                          io=FakeTmux(), inject_fn=fake_inject)
    assert res.ok and "1/2" in res.message
    assert sent == ["%1", "%2"]


def test_button_create():
    c = _parse_btn("create two panes")
    assert c is not None and c.kind == "create" and c.count == 2


def test_keyword_mode_has_no_command_layer():
    # Single-key keyword mode no longer parses commands; only the broadcast word
    # leads, everything else is None (router / verbatim dictation handles it).
    assert _parse("create two panes") is None
    assert _parse("close the others") is None
    assert _parse("flibbertigibbet") is None


def test_button_broadcast_word_still_works():
    c = _parse_btn("everyone run the tests")
    assert c is not None and c.kind == "broadcast" and c.text == "run the tests"


def test_button_macro():
    cfg = Config()
    object.__setattr__(cfg, "macros", {"dev layout": ["create 3 claude panes", "tile"]})
    c = _parse_btn("Dev Layout", cfg)
    assert c is not None and c.kind == "macro"


def test_button_name_address_falls_through_to_none():
    # "nova, are you there?" is not a command -> route+inject, NOT unknown.
    assert _parse_btn("nova are you there") is None


def test_button_gibberish_falls_through_to_none():
    assert _parse_btn("flibbertigibbet") is None


def test_handle_command_button_returns_none_for_non_command():
    res = handle_command("nova hi there", FakeRegistry([]), Config(), addressing="button")
    assert res is None


def test_handle_command_button_executes_create():
    focused = _pane("%0", "%0", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(new_ids=["%1", "%2"])
    res = handle_command("create two panes", reg, Config(),
                         io=io, inject_fn=lambda *a, **k: True, addressing="button")
    assert res is not None and res.ok


# --- zoom / unzoom -----------------------------------------------------------

def test_parse_zoom_focused():
    c = _parse_btn("zoom")
    assert c.kind == "zoom" and c.name == ""


def test_parse_zoom_by_name():
    c = _parse_btn("zoom nova")
    assert c.kind == "zoom" and c.name == "nova"


def test_parse_zoom_synonyms():
    assert _parse_btn("maximize").kind == "zoom"
    assert _parse_btn("full screen").kind == "zoom"
    assert _parse_btn("full screen nova") == Command(kind="zoom", name="nova")


def test_parse_unzoom_synonyms():
    assert _parse_btn("unzoom").kind == "unzoom"
    assert _parse_btn("minimize").kind == "unzoom"
    assert _parse_btn("restore").kind == "unzoom"


def test_parse_unzoom_misheard_split():
    # Parakeet renders "unzoom" as "and zoom" / "un zoom"; a trailing name is
    # ignored (zoom is window-level, only one pane can be zoomed).
    assert _parse_btn("and zoom").kind == "unzoom"
    assert _parse_btn("un zoom").kind == "unzoom"
    assert _parse_btn("and zoom sage").kind == "unzoom"


def test_execute_zoom_focused_selects_then_zooms():
    focused = _pane("%1", "nova", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux()  # not zoomed
    res = execute_command(Command(kind="zoom"), reg, Config(), io=io)
    assert res.ok
    assert io.calls == [("select_pane", "%1"), ("pane_zoomed", "%1"),
                        ("toggle_zoom", "%1")]


def test_execute_zoom_by_name():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])
    io = FakeTmux()
    res = execute_command(Command(kind="zoom", name="atlas"), reg, Config(), io=io)
    assert res.ok
    assert io.calls == [("select_pane", "%2"), ("pane_zoomed", "%2"),
                        ("toggle_zoom", "%2")]


def test_execute_zoom_already_zoomed_is_noop_toggle():
    focused = _pane("%1", "nova", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(zoomed=True)
    res = execute_command(Command(kind="zoom"), reg, Config(), io=io)
    assert res.ok
    assert ("toggle_zoom", "%1") not in io.calls


def test_execute_zoom_unknown_name():
    reg = FakeRegistry([_pane("%1", "nova", active=True)])
    res = execute_command(Command(kind="zoom", name="zzzz"), reg, Config(), io=FakeTmux())
    assert res.ok is False


def test_execute_zoom_no_focused():
    reg = FakeRegistry([_pane("%1", "nova")], focused=None)
    io = FakeTmux()
    res = execute_command(Command(kind="zoom"), reg, Config(), io=io)
    assert res.ok is False and io.calls == []


def test_execute_unzoom_when_zoomed():
    focused = _pane("%1", "nova", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(zoomed=True)
    res = execute_command(Command(kind="unzoom"), reg, Config(), io=io)
    assert res.ok and ("toggle_zoom", "%1") in io.calls


def test_execute_unzoom_when_not_zoomed_is_noop():
    focused = _pane("%1", "nova", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(zoomed=False)
    res = execute_command(Command(kind="unzoom"), reg, Config(), io=io)
    assert res.ok and not any(c[0] == "toggle_zoom" for c in io.calls)


def test_execute_unzoom_no_focused():
    reg = FakeRegistry([_pane("%1", "nova")], focused=None)
    io = FakeTmux()
    res = execute_command(Command(kind="unzoom"), reg, Config(), io=io)
    assert res.ok is False and io.calls == []


# --- slash commands ----------------------------------------------------------
# A spoken verb (config slash_commands map) injects a literal Claude Code slash
# command into one pane (focused or named) or all named panes ("... all").

def test_parse_slash_bare_targets_focused():
    c = _parse_btn("clear")
    assert c == Command(kind="slash", text="/clear", name="", to_all=False)


def test_parse_slash_by_name():
    c = _parse_btn("clear nova")
    assert c == Command(kind="slash", text="/clear", name="nova")


def test_parse_slash_all():
    assert _parse_btn("clear all") == Command(kind="slash", text="/clear", to_all=True)
    assert _parse_btn("clear everyone") == Command(kind="slash", text="/clear", to_all=True)


def test_parse_slash_compact_default():
    c = _parse_btn("compact")
    assert c.kind == "slash" and c.text == "/compact"


def test_parse_slash_verb_not_in_map_falls_through():
    # "model" is not a default slash command -> not a command -> None.
    assert _parse_btn("model") is None


def test_button_slash_by_name():
    c = _parse_btn("clear nova")
    assert c == Command(kind="slash", text="/clear", name="nova")


def test_button_slash_all():
    assert _parse_btn("clear all") == Command(kind="slash", text="/clear", to_all=True)


def test_button_slash_bare_targets_focused():
    c = _parse_btn("clear")
    assert c == Command(kind="slash", text="/clear", name="", to_all=False)


def test_button_slash_verb_not_in_map_falls_through():
    # An unmapped verb in button mode routes+injects, not unknown.
    assert _parse_btn("model") is None


def test_execute_slash_to_all_named_panes():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas"),
             _pane("%3", "%3")]  # %3 unnamed -> skipped
    reg = FakeRegistry(panes, focused=panes[0])
    sent = []

    def fake_inject(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05):
        sent.append((pane_id, text))
        return True

    res = execute_command(Command(kind="slash", text="/clear", to_all=True),
                          reg, Config(), io=FakeTmux(), inject_fn=fake_inject)
    assert res.ok and "2/2" in res.message
    assert sent == [("%1", "/clear"), ("%2", "/clear")]


def test_execute_slash_to_all_no_named_agents():
    reg = FakeRegistry([_pane("%1", "%1", active=True)])
    res = execute_command(Command(kind="slash", text="/clear", to_all=True), reg,
                          Config(), io=FakeTmux(), inject_fn=lambda *a, **k: True)
    assert res.ok is False


def test_execute_slash_by_name_injects_literal():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])
    sent = []
    res = execute_command(
        Command(kind="slash", text="/clear", name="atlas"), reg, Config(),
        io=FakeTmux(),
        inject_fn=lambda pid, txt, **k: sent.append((pid, txt)) or True)
    assert res.ok and sent == [("%2", "/clear")]


def test_execute_slash_by_name_unknown():
    reg = FakeRegistry([_pane("%1", "nova", active=True)])
    res = execute_command(Command(kind="slash", text="/clear", name="zzzz"), reg,
                          Config(), io=FakeTmux(), inject_fn=lambda *a, **k: True)
    assert res.ok is False


def test_execute_slash_by_name_ambiguous_does_not_inject():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "novo")]
    sent = []
    res = execute_command(
        Command(kind="slash", text="/clear", name="nov"),
        FakeRegistry(panes, focused=panes[0]), Config(), io=FakeTmux(),
        inject_fn=lambda pid, txt, **k: sent.append(pid) or True)
    assert res.ok is False and sent == []


def test_execute_slash_focused_injects_literal():
    focused = _pane("%1", "nova", active=True)
    reg = FakeRegistry([focused], focused=focused)
    sent = []
    res = execute_command(
        Command(kind="slash", text="/clear"), reg, Config(), io=FakeTmux(),
        inject_fn=lambda pid, txt, **k: sent.append((pid, txt)) or True)
    assert res.ok and sent == [("%1", "/clear")]


def test_execute_slash_focused_no_focus():
    reg = FakeRegistry([_pane("%1", "nova")], focused=None)
    res = execute_command(Command(kind="slash", text="/clear"), reg, Config(),
                          io=FakeTmux(), inject_fn=lambda *a, **k: True)
    assert res.ok is False


def test_execute_slash_injection_failure_reports_not_ok():
    focused = _pane("%1", "nova", active=True)
    reg = FakeRegistry([focused], focused=focused)
    res = execute_command(Command(kind="slash", text="/clear"), reg, Config(),
                          io=FakeTmux(), inject_fn=lambda *a, **k: False)
    assert res.ok is False


# --- vocative filler peel before command verbs (button mode) ------------------
# "okay focus nova" / "um create two panes": a leading filler is peeled before
# the verb. Broadcast is NOT peeled (mass-broadcast blast radius). A non-command
# after peeling still falls through to None (router/inject handles it).

def test_button_filler_then_create():
    c = _parse_btn("okay create two panes")
    assert c is not None and c.kind == "create" and c.count == 2


def test_button_two_fillers_then_focus():
    c = _parse_btn("um okay focus nova")
    assert c is not None and c.kind == "focus" and c.name == "nova"


def test_button_filler_then_slash_all():
    c = _parse_btn("hey clear all")
    assert c == Command(kind="slash", text="/clear", to_all=True)


def test_button_filler_then_non_command_falls_through():
    assert _parse_btn("okay just chatting here") is None


def test_button_filler_before_broadcast_is_not_peeled():
    # Broadcast must stay raw-led; "um everyone ..." is not broadcast (no peel),
    # it falls through to None and the router/inject handles it verbatim.
    assert _parse_btn("um everyone deploy") is None
