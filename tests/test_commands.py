import pytest

from vupai.commands import Command, _parse_ssh, execute_command, handle_command, parse_command
from vupai.config import Config
from vupai.registry import Pane


class FakeTmux:
    def __init__(self, new_ids=(), zoomed=False, board_pane=None):
        self.calls = []
        self._ids = list(new_ids)
        self._zoomed = zoomed
        self._board_pane = board_pane

    def split_window(self, target, program, *, horizontal=False, size=None):
        self.calls.append(("split_window", target, program))
        return self._ids.pop(0)

    def find_board_pane(self, session):
        return self._board_pane

    def mark_board_pane(self, pane_id):
        self.calls.append(("mark_board_pane", pane_id))

    def select_layout(self, target, layout):
        self.calls.append(("select_layout", target, layout))

    def set_pane_name(self, pane_id, name):
        self.calls.append(("set_pane_name", pane_id, name))

    def set_pane_program(self, pane_id, label):
        self.calls.append(("set_pane_program", pane_id, label))

    def select_pane(self, pane_id):
        self.calls.append(("select_pane", pane_id))

    def swap_pane(self, a, b, *, detached=False):
        self.calls.append(("swap_pane", a, b, detached))

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


def _pane(id, name, window_id="@1", active=False, session="repo", index=0):
    return Pane(id=id, window_id=window_id, window="main", index=index,
                name=name, command="zsh", active=active, session=session)


def test_wrap_agent_command_drops_to_shell_on_exit():
    from vupai.commands import wrap_agent_command
    # A real program is wrapped so exiting it re-execs an interactive shell.
    assert wrap_agent_command("claude") == "claude; exec ${SHELL:-/bin/sh} -i"
    # Empty (plain-shell default) is left untouched.
    assert wrap_agent_command("") == ""


def test_execute_create_splits_names_and_tiles(monkeypatch):
    monkeypatch.setattr("vupai.commands.shutil.which", lambda c: "/bin/claude")
    focused = _pane("%0", "%0", active=True)  # unnamed focused pane
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(new_ids=["%1", "%2"])
    cmd = Command(kind="create", count=2, program=None, unit="pane")
    res = execute_command(cmd, reg, Config(), io=io)
    assert res.ok
    # default program, wrapped so the pane survives the agent's exit
    assert ("split_window", "@1", "claude; exec ${SHELL:-/bin/sh} -i") in io.calls
    assert ("set_pane_name", "%1", "nova") in io.calls
    assert ("set_pane_name", "%2", "atlas") in io.calls
    # program label stored separately so the border survives the agent
    # overwriting pane_title with its own summary
    assert ("set_pane_program", "%1", "claude") in io.calls
    assert ("set_pane_program", "%2", "claude") in io.calls
    assert io.calls[-1] == ("select_layout", "@1", "tiled")


def test_execute_create_retiles_after_every_split(monkeypatch):
    # Re-tile after each split so tmux redistributes space and never hits
    # "no space for new pane" mid-create (regression: was tiled only once).
    monkeypatch.setattr("vupai.commands.shutil.which", lambda c: "/bin/claude")
    focused = _pane("%0", "%0", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(new_ids=["%1", "%2", "%3", "%4", "%5", "%6", "%7"])
    cmd = Command(kind="create", count=7, program=None, unit="pane")
    res = execute_command(cmd, reg, Config(), io=io)
    assert res.ok
    splits = [c for c in io.calls if c[0] == "split_window"]
    tiles = [c for c in io.calls if c == ("select_layout", "@1", "tiled")]
    assert len(splits) == 7
    # one tile per split keeps room for the next one
    assert len(tiles) >= 7


def test_execute_create_falls_back_to_shell_when_program_missing(monkeypatch):
    # A named program that isn't on PATH degrades to a shell (panes still get
    # created + named), with a note instead of spawning panes that exit at once.
    monkeypatch.setattr("vupai.commands.shutil.which", lambda c: None)
    focused = _pane("%0", "%0", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(new_ids=["%1"])
    cmd = Command(kind="create", count=1, program="codex", unit="pane")
    res = execute_command(cmd, reg, Config(), io=io)
    assert res.ok
    assert ("split_window", "@1", "") in io.calls   # degraded to a plain shell
    assert "codex" in res.message and "shell" in res.message
    assert ("set_pane_name", "%1", "nova") in io.calls
    # degraded to a shell -> empty program label (border omits the segment)
    assert ("set_pane_program", "%1", "") in io.calls


def test_program_label_reduces_to_basename():
    from vupai.commands import program_label
    assert program_label("claude") == "claude"
    assert program_label("/usr/bin/codex --foo bar") == "codex"
    assert program_label("  opencode  ") == "opencode"
    # plain-shell default -> empty so the border omits the program segment
    assert program_label("") == ""


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


def test_parse_create_spoken_count_past_nine():
    c = _parse_btn("create ten panes")
    assert c.kind == "create" and c.count == 10
    c = _parse_btn("create twelve panes")
    assert c.kind == "create" and c.count == 12


def test_parse_create_digit_count_past_nine():
    c = _parse_btn("create 16 panes")
    assert c.kind == "create" and c.count == 16


def test_parse_create_at_max_count():
    from vupai.commands import MAX_CREATE_COUNT

    c = _parse_btn(f"create {MAX_CREATE_COUNT} panes")
    assert c.kind == "create" and c.count == MAX_CREATE_COUNT


def test_parse_create_over_max_count_rejected():
    # Past the safety cap the create parse fails, so the utterance is not a
    # create command (falls through to unknown - never spawns a runaway count).
    from vupai.commands import MAX_CREATE_COUNT

    c = _parse_btn(f"create {MAX_CREATE_COUNT + 1} panes")
    assert c is None or c.kind != "create"


def test_parse_create_article_means_one():
    # "create a pane" == "create one pane".
    c = _parse_btn("create a pane")
    assert c.kind == "create" and c.count == 1 and c.program is None and c.unit == "pane"


def test_parse_create_article_an_with_program():
    c = _parse_btn("create an shell pane")
    assert c.kind == "create" and c.count == 1 and c.program == "" and c.unit == "pane"


def test_parse_create_another_means_one():
    c = _parse_btn("create another pane")
    assert c.kind == "create" and c.count == 1 and c.unit == "pane"


def test_parse_close_everyone_closes_others():
    # "close everyone" aligns with the slash all-target grammar, not a pane named
    # "everyone".
    for word in ("others", "rest", "all", "everyone", "everybody"):
        c = _parse_btn(f"close {word}")
        assert c.kind == "close_others", word


def test_parse_create_new_filler_is_ignored():
    # "create a new pane" == "create a pane" - "new" is descriptive filler.
    c = _parse_btn("create a new pane")
    assert c.kind == "create" and c.count == 1 and c.program is None and c.unit == "pane"


def test_parse_create_new_filler_with_count():
    c = _parse_btn("create two new panes")
    assert c.kind == "create" and c.count == 2 and c.program is None


def test_parse_create_new_filler_with_program():
    c = _parse_btn("create two new shell panes")
    assert c.kind == "create" and c.count == 2 and c.program == ""


def test_parse_create_quick_filler_is_ignored():
    c = _parse_btn("create a quick pane")
    assert c.kind == "create" and c.count == 1 and c.unit == "pane"


def test_parse_create_explicit_shell_program():
    c = _parse_btn("create two shell panes")
    assert c.kind == "create" and c.count == 2 and c.program == ""


def test_parse_create_explicit_codex_program():
    # A non-default agent is selectable by a single program token from `programs`.
    c = _parse_btn("create two codex panes")
    assert c.kind == "create" and c.count == 2 and c.program == "codex"


def test_parse_create_codex_homophone():
    # "codex" mishears as "codecs"/"codec" - the curated alias recovers it. This
    # is the reported failure: "open two codex" lands as "open two codecs".
    for spoken in ("open two codecs panes", "open two codec", "create a codecs",
                   "open one colex", "open one co"):
        c = _parse_btn(spoken)
        assert c is not None and c.kind == "create" and c.program == "codex", spoken


def test_parse_create_opencode_split_phrase():
    # "opencode" is transcribed as the two-token split "open code"; the phrase
    # alias recovers it even though "open" is itself a create verb.
    c = _parse_btn("create two open code panes")
    assert c is not None and c.kind == "create" and c.count == 2 and c.program == "opencode"
    c = _parse_btn("open two open code")
    assert c is not None and c.kind == "create" and c.count == 2 and c.program == "opencode"


def test_parse_create_windows_unit():
    c = _parse_btn("make two windows")
    assert c.kind == "create" and c.count == 2 and c.unit == "window"


def test_parse_create_unknown_program_falls_through():
    # An unrecognized program is not a command -> None (router/inject handles it).
    assert _parse_btn("create two banana panes") is None


# --- optional unit noun + homophone-free synonyms ----------------------------
# "pane" mishears as "pain"/"panel"; users can drop the noun entirely or say a
# clean synonym ("agent"/"split") instead.

def test_parse_create_noun_optional_plural():
    c = _parse_btn("create two")
    assert c.kind == "create" and c.count == 2 and c.program is None and c.unit == "pane"


def test_parse_create_noun_optional_singular():
    c = _parse_btn("create a")
    assert c.kind == "create" and c.count == 1 and c.unit == "pane"


def test_parse_create_noun_optional_spin_up():
    c = _parse_btn("spin up three")
    assert c.kind == "create" and c.count == 3 and c.unit == "pane"


def test_parse_create_noun_optional_with_program():
    c = _parse_btn("create two shell")
    assert c.kind == "create" and c.count == 2 and c.program == "" and c.unit == "pane"


def test_parse_create_agent_synonym_is_pane():
    c = _parse_btn("create two agents")
    assert c.kind == "create" and c.count == 2 and c.unit == "pane"


def test_parse_create_split_synonym_is_pane():
    c = _parse_btn("create three splits")
    assert c.kind == "create" and c.count == 3 and c.unit == "pane"


def test_parse_create_agent_singular_with_program():
    c = _parse_btn("create a codex agent")
    assert c.kind == "create" and c.count == 1 and c.program == "codex" and c.unit == "pane"


def test_parse_create_bare_verb_not_a_command():
    # "create" alone has no count -> falls through (router/inject), not a create.
    assert _parse_btn("create") is None


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


# --- ASR homophone tolerance for the lead verb (ate/hate/eight/crate) --------

def test_parse_create_misheard_verb_crate():
    c = _parse_btn("crate two panes")
    assert c.kind == "create" and c.count == 2 and c.unit == "pane"


def test_parse_create_misheard_verb_ate():
    c = _parse_btn("ate three panes")
    assert c.kind == "create" and c.count == 3 and c.unit == "pane"


def test_parse_create_misheard_verb_creator():
    c = _parse_btn("creator two panes")
    assert c.kind == "create" and c.count == 2 and c.unit == "pane"


def test_parse_create_misheard_verb_eight():
    # "create two" -> "eight two"; the verb alias is consumed, count follows.
    c = _parse_btn("eight two")
    assert c.kind == "create" and c.count == 2 and c.unit == "pane"


def test_parse_create_misheard_verb_hate_with_program():
    c = _parse_btn("hate two shell panes")
    assert c.kind == "create" and c.count == 2 and c.program == "" and c.unit == "pane"


def test_misheard_verb_without_count_falls_through():
    # A real-word homophone that isn't a create: no count -> not a command.
    assert _parse_btn("hate this code") is None
    assert _parse_btn("ate lunch") is None


def test_parse_create_misheard_pens_is_pane():
    # "create four panes" -> "create four pens".
    c = _parse_btn("create four pens")
    assert c.kind == "create" and c.count == 4 and c.unit == "pane"
    c = _parse_btn("create a pen")
    assert c.kind == "create" and c.count == 1 and c.unit == "pane"


def test_parse_create_misheard_with_program():
    # A misheard unit still composes with a valid mid-token program.
    c = _parse_btn("create two shell pains")
    assert c.kind == "create" and c.count == 2 and c.program == "" and c.unit == "pane"


def test_resolve_unit_exact_and_aliases():
    from vupai.commands import _resolve_unit
    assert _resolve_unit("panes") == "pane"     # exact path still wins
    assert _resolve_unit("windows") == "window"
    assert _resolve_unit("paints") == "pane"     # alias
    assert _resolve_unit("pains") == "pane"


def test_resolve_unit_rejects_real_word_lookalikes():
    # Precision guard: real English words that merely rhyme must NOT be units,
    # or "create three lanes"/"create four plans" would spawn panes.
    from vupai.commands import _resolve_unit
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


def test_parse_focus_strips_leading_the():
    # "focus the nova" / "switch to the nova" must address nova, not "the".
    assert _parse_btn("focus the nova") == Command(kind="focus", name="nova")
    assert _parse_btn("switch to the nova") == Command(kind="focus", name="nova")


def test_parse_swap_strips_the():
    assert _parse_btn("swap the nova and the atlas") == Command(
        kind="swap", name="nova", name_b="atlas")


def test_parse_zoom_strips_the():
    assert _parse_btn("zoom the nova") == Command(kind="zoom", name="nova")


def test_parse_swap_misheard_verb():
    # "swap nova atlas" -> "swab/swamp nova atlas"; verb alias resolves to swap.
    assert _parse_btn("swab nova atlas") == Command(kind="swap", name="nova", name_b="atlas")
    assert _parse_btn("swamp nova atlas").kind == "swap"


def test_parse_swap_misheard_verb_needs_two_names():
    # A bare homophone (or one with a single token) is not a swap -> falls through.
    assert _parse_btn("swamp") is None
    assert _parse_btn("swab nova") is None


def test_execute_swap_two_named_panes():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])
    io = FakeTmux()
    res = execute_command(Command(kind="swap", name="nova", name_b="atlas"),
                          reg, Config(), io=io)
    assert res.ok and io.calls == [("swap_pane", "%1", "%2", False)]


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


def test_parse_close_misheard_verb_clothes():
    # "close nova" -> "clothes nova"; the verb alias resolves to a close.
    c = _parse_btn("clothes nova")
    assert c.kind == "close" and c.name == "nova"
    assert _parse_btn("cloze nova").kind == "close"


def test_parse_close_misheard_verb_rose():
    # "close nova" lands as "rose nova" in the wild.
    c = _parse_btn("rose nova")
    assert c.kind == "close" and c.name == "nova"


def test_parse_close_misheard_verb_closed():
    # Past-tense mishearing seen in the wild ("close juno" -> "closed juno").
    c = _parse_btn("closed nova")
    assert c.kind == "close" and c.name == "nova"
    # Bare "closed" has no target -> non-destructive fall-through.
    assert _parse_btn("closed") is None


def test_parse_close_misheard_verb_all_target():
    assert _parse_btn("clothes all").kind == "close_others"


def test_parse_bare_misheard_close_falls_through():
    # A homophone with no target is not destructive -> falls through to inject.
    assert _parse_btn("clothes") is None


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


def test_execute_close_others_spares_other_sessions():
    # Server-wide registry, but close-all must stay in the focused session so it
    # can't kill another repo's panes.
    focused = _pane("%1", "nova", active=True, session="repoA")
    panes = [focused, _pane("%2", "atlas", session="repoA"),
             _pane("%3", "orion", session="repoB")]
    reg = FakeRegistry(panes, focused=focused)
    io = FakeTmux()
    res = execute_command(Command(kind="close_others"), reg, Config(), io=io)
    assert res.ok
    assert io.calls == [("kill_pane", "%2")]  # %3 (repoB) untouched


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
    focused = _pane("%1", "%1", active=True)
    reg = FakeRegistry([focused], focused=focused)
    res = execute_command(Command(kind="broadcast", text="hi"), reg, Config(),
                          io=FakeTmux(), inject_fn=lambda *a, **k: True)
    assert res.ok is False


def test_execute_broadcast_stays_in_focused_session():
    focused = _pane("%1", "nova", active=True, session="repoA")
    panes = [focused, _pane("%2", "atlas", session="repoA"),
             _pane("%3", "orion", session="repoB")]
    reg = FakeRegistry(panes, focused=focused)
    sent = []

    def fake_inject(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05):
        sent.append((pane_id, text))
        return True

    res = execute_command(Command(kind="broadcast", text="go"),
                          reg, Config(), io=FakeTmux(), inject_fn=fake_inject)
    assert res.ok and "2/2" in res.message  # only repoA agents
    assert sent == [("%1", "go"), ("%2", "go")]


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


def test_parse_zoom_misheard_verb_zoo():
    assert _parse_btn("zoo").kind == "zoom"
    assert _parse_btn("zoo nova") == Command(kind="zoom", name="nova")
    # "zoom atlas" lands as "boom atlas" in the wild.
    assert _parse_btn("boom").kind == "zoom"
    assert _parse_btn("boom atlas") == Command(kind="zoom", name="atlas")


def test_parse_unzoom_synonyms():
    assert _parse_btn("unzoom").kind == "unzoom"
    assert _parse_btn("minimize").kind == "unzoom"
    assert _parse_btn("restore").kind == "unzoom"
    # "minimize" lands as the single token "miniways" in the wild.
    assert _parse_btn("miniways").kind == "unzoom"


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
    focused = _pane("%1", "%1", active=True)
    reg = FakeRegistry([focused], focused=focused)
    res = execute_command(Command(kind="slash", text="/clear", to_all=True), reg,
                          Config(), io=FakeTmux(), inject_fn=lambda *a, **k: True)
    assert res.ok is False


def test_execute_slash_to_all_stays_in_focused_session():
    focused = _pane("%1", "nova", active=True, session="repoA")
    panes = [focused, _pane("%2", "atlas", session="repoA"),
             _pane("%3", "orion", session="repoB")]
    reg = FakeRegistry(panes, focused=focused)
    sent = []

    def fake_inject(pane_id, text, *, confirm_timeout=2.0, poll_interval=0.05):
        sent.append((pane_id, text))
        return True

    res = execute_command(Command(kind="slash", text="/clear", to_all=True),
                          reg, Config(), io=FakeTmux(), inject_fn=fake_inject)
    assert res.ok and "2/2" in res.message  # only repoA agents
    assert sent == [("%1", "/clear"), ("%2", "/clear")]


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


# --- layout commands ----------------------------------------------------------

def test_parse_layout_grid_and_aliases():
    for word in ("grid", "tile", "tiled", "tiles", "bento"):
        c = _parse_btn(f"layout {word}")
        assert c is not None and c.kind == "layout"
        assert c.layout == "tiled" and c.main_focus is False


def test_parse_layout_focus_left_variants():
    for phrase in ("left", "focus left", "main left", "stack right"):
        c = _parse_btn(f"layout {phrase}")
        assert c.kind == "layout" and c.layout == "main-vertical" and c.main_focus is True


def test_parse_layout_focus_top_variants():
    for phrase in ("top", "focus top", "main top", "stack bottom"):
        c = _parse_btn(f"layout {phrase}")
        assert c.kind == "layout" and c.layout == "main-horizontal" and c.main_focus is True


def test_parse_layout_even_splits():
    for phrase in ("columns", "even columns"):
        c = _parse_btn(f"layout {phrase}")
        assert c.kind == "layout" and c.layout == "even-horizontal" and c.main_focus is False
    for phrase in ("rows", "even rows"):
        c = _parse_btn(f"layout {phrase}")
        assert c.kind == "layout" and c.layout == "even-vertical" and c.main_focus is False


def test_parse_layout_two_token_verb_and_filler():
    assert _parse_btn("lay out grid").layout == "tiled"
    assert _parse_btn("okay lay out grid").layout == "tiled"
    assert _parse_btn("okay layout grid").layout == "tiled"
    assert _parse_btn("layout the grid").layout == "tiled"


def test_parse_layout_plural_verb_alias():
    # "layouts" is the curated plural mishearing of the "layout" lead verb.
    assert _parse_btn("layouts grid").layout == "tiled"


def test_parse_layout_unknown_or_bare_verb_falls_through():
    assert _parse_btn("layout") is None
    assert _parse_btn("layout wobble") is None
    # Bare "lay" is not the verb (requires the full ["lay","out"]).
    assert _parse_btn("lay down") is None
    assert _parse_btn("lay off the gas") is None


def test_parse_layout_names_need_the_lead_verb():
    # Structural invariant: a layout name with no "layout" lead is dictation,
    # never a layout command (and must not be hijacked by another verb).
    for word in ("grid", "bento", "left", "top", "even", "stack", "columns", "rows"):
        assert _parse_btn(word) is None


def test_parse_layout_is_button_mode_only():
    # keyword mode has no command layer.
    assert _parse("layout grid") is None


# --- board -------------------------------------------------------------------

def test_parse_board_bare_and_lead_verbs():
    for phrase in ("board", "open board", "create board", "show board",
                   "make board", "new board", "the board", "open the board"):
        c = _parse_btn(phrase)
        assert c == Command(kind="board"), phrase


def test_parse_board_requires_the_board_noun():
    # A lead verb without "board" is not a board command (create needs a count;
    # bare "show" is dictation).
    assert _parse_btn("show") is None
    assert _parse_btn("open the nova") is None  # -> not a board command


def test_parse_board_is_button_mode_only():
    assert _parse("board") is None


def test_exec_board_opens_pane_off_focused():
    focused = _pane("%0", "nova", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(new_ids=["%7"])
    res = handle_command("open board", reg, Config(), io=io,
                         inject_fn=lambda *a, **k: True, addressing="button")
    assert res is not None and res.ok
    splits = [c for c in io.calls if c[0] == "split_window"]
    assert len(splits) == 1 and splits[0][2].endswith("_board")
    assert ("mark_board_pane", "%7") in io.calls
    assert ("set_pane_name", "%7", "board") in io.calls


def test_exec_board_focuses_existing_instead_of_second_split():
    focused = _pane("%0", "nova", active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux(board_pane="%5")          # a board already exists in the session
    res = handle_command("board", reg, Config(), io=io,
                         inject_fn=lambda *a, **k: True, addressing="button")
    assert res is not None and res.ok
    assert [c for c in io.calls if c[0] == "split_window"] == []
    assert ("select_pane", "%5") in io.calls


def test_exec_board_no_focused_pane():
    reg = FakeRegistry([], focused=None)
    res = handle_command("board", reg, Config(), io=FakeTmux(),
                         inject_fn=lambda *a, **k: True, addressing="button")
    assert res is not None and res.ok is False


# --- layout executor tests ---------------------------------------------------

def test_execute_layout_grid_no_swap():
    panes = [_pane("%1", "nova", index=0, active=True), _pane("%2", "atlas", index=1)]
    reg = FakeRegistry(panes, focused=panes[0])
    io = FakeTmux()
    res = execute_command(Command(kind="layout", layout="tiled", main_focus=False),
                          reg, Config(), io=io)
    assert res.ok and "layout" in res.message and "grid" in res.message
    assert io.calls == [("select_layout", "@1", "tiled")]


def test_execute_layout_main_left_swaps_focused_into_main_then_lays_out():
    main = _pane("%1", "nova", index=0)
    focused = _pane("%2", "atlas", index=1, active=True)
    reg = FakeRegistry([main, focused], focused=focused)
    io = FakeTmux()
    res = execute_command(Command(kind="layout", layout="main-vertical", main_focus=True),
                          reg, Config(), io=io)
    assert res.ok and "main left" in res.message
    # swap focused (%2) into the main slot (%1), detached, THEN select-layout.
    assert io.calls == [("swap_pane", "%2", "%1", True),
                        ("select_layout", "@1", "main-vertical")]


def test_execute_layout_main_picks_lowest_index_with_gaps():
    # Non-contiguous indices: the min-index pane (%a, index 2) is the main slot.
    main = _pane("%a", "nova", index=2)
    focused = _pane("%b", "atlas", index=5, active=True)
    reg = FakeRegistry([focused, main], focused=focused)  # list order != index order
    io = FakeTmux()
    execute_command(Command(kind="layout", layout="main-vertical", main_focus=True),
                    reg, Config(), io=io)
    assert io.calls[0] == ("swap_pane", "%b", "%a", True)


def test_execute_layout_main_focused_already_main_skips_swap():
    focused = _pane("%1", "nova", index=0, active=True)
    other = _pane("%2", "atlas", index=1)
    reg = FakeRegistry([focused, other], focused=focused)
    io = FakeTmux()
    execute_command(Command(kind="layout", layout="main-horizontal", main_focus=True),
                    reg, Config(), io=io)
    assert io.calls == [("select_layout", "@1", "main-horizontal")]


def test_execute_layout_single_pane_is_noop():
    focused = _pane("%1", "nova", index=0, active=True)
    reg = FakeRegistry([focused], focused=focused)
    io = FakeTmux()
    res = execute_command(Command(kind="layout", layout="tiled", main_focus=False),
                          reg, Config(), io=io)
    assert res.ok and "nothing to arrange" in res.message
    assert io.calls == []


def test_execute_layout_only_counts_focused_window():
    # Focused window @1 has one pane; window @2 has three. Must be a single-pane no-op.
    focused = _pane("%1", "nova", window_id="@1", index=0, active=True)
    others = [_pane("%2", "a", window_id="@2", index=0),
              _pane("%3", "b", window_id="@2", index=1),
              _pane("%4", "c", window_id="@2", index=2)]
    reg = FakeRegistry([focused, *others], focused=focused)
    io = FakeTmux()
    res = execute_command(Command(kind="layout", layout="tiled", main_focus=False),
                          reg, Config(), io=io)
    assert res.ok and "nothing to arrange" in res.message
    assert io.calls == []


def test_execute_layout_no_focused_pane():
    reg = FakeRegistry([_pane("%1", "nova", index=0)], focused=None)
    io = FakeTmux()
    res = execute_command(Command(kind="layout", layout="tiled", main_focus=False),
                          reg, Config(), io=io)
    assert res.ok is False and res.message == "no focused pane" and io.calls == []


# --- read command: parsing --------------------------------------------------


def test_parse_read_by_name():
    assert _parse_btn("read nova") == Command(kind="read", name="nova")


def test_parse_read_bare_targets_focused():
    assert _parse_btn("read") == Command(kind="read", name="")


def test_parse_read_strips_article_and_filler():
    assert _parse_btn("read the nova").name == "nova"
    assert _parse_btn("read me nova").name == "nova"
    assert _parse_btn("read out atlas").name == "atlas"


def test_parse_read_misheard_verbs():
    # "read" lands as the homophone "reed" or the past-tense spelling "red".
    assert _parse_btn("reed nova").kind == "read"
    assert _parse_btn("red nova") == Command(kind="read", name="nova")


def test_parse_read_misheard_as_reve_reeve_wreath():
    # Real ASR mishearings of "read <name>" seen in the journal: parakeet lands
    # "read" as "reve" / "reeve" / "wreath", which otherwise fall through to
    # routing and report not_addressed.
    assert _parse_btn("reve echo") == Command(kind="read", name="echo")
    assert _parse_btn("reeve echo") == Command(kind="read", name="echo")
    assert _parse_btn("wreath sage") == Command(kind="read", name="sage")


def test_parse_read_only_on_button_key():
    # Read is a system-key command; the dictation/keyword key types verbatim.
    assert _parse("read nova") is None


def test_parse_read_board_is_a_digest_not_a_pane_named_board():
    # "read board" speaks every agent's status, it does NOT look up a pane "board".
    assert _parse_btn("read board") == Command(kind="read", to_all=True)
    assert _parse_btn("read the board") == Command(kind="read", to_all=True)


def test_parse_read_all_is_a_digest():
    assert _parse_btn("read all") == Command(kind="read", to_all=True)
    assert _parse_btn("read everyone") == Command(kind="read", to_all=True)


# --- read command: execution ------------------------------------------------


def _summary(text, needs_input=False):
    from vupai.summarize import Summary
    return Summary(text, needs_input, "llm")


def test_execute_read_named_pane_summarizes_and_speaks():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])  # focus is nova...
    spoken, captured = [], []
    res = execute_command(
        Command(kind="read", name="atlas"), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: captured.append(pid) or "scrollback",
        title_fn=lambda pid: "",
        summarize_fn=lambda tail, _title: _summary("ran the suite, all green"),
        speak_fn=lambda text: spoken.append(text))
    assert res.ok
    assert captured == ["%2"]  # ...but the NAMED pane is read, not the focused one
    assert res.message == "atlas: ran the suite, all green"
    assert spoken == ["atlas: ran the suite, all green"]


def test_execute_read_bare_reads_focused_pane():
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])
    spoken = []
    res = execute_command(
        Command(kind="read", name=""), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "nova tail", title_fn=lambda pid: "",
        summarize_fn=lambda tail, _title: _summary("waiting for input", needs_input=True),
        speak_fn=lambda t: spoken.append(t))
    assert res.ok and res.message == "nova: waiting for input"
    assert spoken == ["nova: waiting for input"]


def test_execute_read_unnamed_focused_pane_has_no_prefix():
    focused = _pane("%5", "%5", active=True)  # unnamed: name == id
    reg = FakeRegistry([focused], focused=focused)
    spoken = []
    res = execute_command(
        Command(kind="read", name=""), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "shell tail", title_fn=lambda pid: "",
        summarize_fn=lambda tail, _title: _summary("idle shell"),
        speak_fn=lambda t: spoken.append(t))
    assert res.ok and res.message == "idle shell"  # no "<label>: " prefix
    assert spoken == ["idle shell"]


def test_execute_read_unknown_pane():
    reg = FakeRegistry([_pane("%1", "nova", active=True)], focused=None)
    res = execute_command(
        Command(kind="read", name="ghost"), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "x", summarize_fn=lambda tail, _title: _summary("x"),
        speak_fn=lambda t: None)
    assert not res.ok and "no pane named ghost" in res.message


def test_execute_read_no_focused_pane():
    reg = FakeRegistry([], focused=None)
    res = execute_command(
        Command(kind="read", name=""), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "x", summarize_fn=lambda tail, _title: _summary("x"),
        speak_fn=lambda t: None)
    assert not res.ok and "no focused pane" in res.message


def test_execute_read_capture_failure_is_reported_not_raised():
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])

    def boom(pid):
        raise RuntimeError("pane vanished mid-read")

    res = execute_command(
        Command(kind="read", name="nova"), reg, Config(), io=FakeTmux(),
        capture_fn=boom, summarize_fn=lambda tail, _title: _summary("x"),
        speak_fn=lambda t: None)
    assert not res.ok and "couldn't read nova" in res.message


def test_execute_read_ambiguous_names(monkeypatch):
    from vupai.router import NameMatch
    panes = [_pane("%1", "nova"), _pane("%2", "norma")]
    reg = FakeRegistry(panes, focused=panes[0])
    monkeypatch.setattr(
        "vupai.commands.resolve_pane_by_name",
        lambda *a, **k: NameMatch(None, None, 83.0, ("nova", "norma")))
    res = execute_command(
        Command(kind="read", name="nor"), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "x", summarize_fn=lambda tail, _title: _summary("x"),
        speak_fn=lambda t: None)
    assert not res.ok and "ambiguous" in res.message and "nova" in res.message


def test_execute_read_tts_disabled_returns_summary_but_stays_silent(monkeypatch):
    # With tts off the default speaker is a no-op, yet the summary still surfaces
    # (as the CommandResult message -> the status line). No real audio spawns.
    spawned = []
    monkeypatch.setattr(
        "vupai.speech.subprocess.Popen", lambda *a, **k: spawned.append(a))
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])
    res = execute_command(
        Command(kind="read", name="nova"), reg, Config(tts_enabled=False),
        io=FakeTmux(), capture_fn=lambda pid: "tail", title_fn=lambda pid: "",
        summarize_fn=lambda tail, _title: _summary("done"))
    assert res.ok and res.message == "nova: done"
    assert spawned == []


def test_default_speaker_noop_when_disabled():
    from vupai.commands import _default_speaker
    assert _default_speaker(Config(tts_enabled=False))("hello") is None
    assert _default_speaker(Config(tts_enabled=True, tts_cmd=""))("hello") is None


def test_default_speaker_calls_speech_when_enabled(monkeypatch):
    from vupai.commands import _default_speaker
    calls = []
    monkeypatch.setattr(
        "vupai.speech.speak", lambda text, *, cmd: calls.append((text, cmd)))
    _default_speaker(Config(tts_enabled=True, tts_cmd="say -v Daniel"))("hello")
    assert calls == [("hello", "say -v Daniel")]


def test_bound_tail_limits_lines_then_keeps_the_end():
    from vupai.commands import _READ_CAPTURE_LINES, _bound_tail
    bounded = _bound_tail("\n".join(f"line{i}" for i in range(100)))
    assert bounded.count("\n") + 1 == _READ_CAPTURE_LINES
    assert bounded.endswith("line99")


def test_execute_read_board_digest_speaks_every_agent():
    from vupai.board import PaneStatus
    from vupai.panestate import PaneState
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])
    seen, spoken = [], []
    statuses = [
        PaneStatus("nova", "claude", PaneState.WORKING, "fixing the parser", False),
        PaneStatus("atlas", "claude", PaneState.NEEDS_INPUT, "approve deploy?", True),
    ]
    res = execute_command(
        Command(kind="read", to_all=True), reg, Config(), io=FakeTmux(),
        statuses_fn=lambda p: seen.append(p) or statuses,
        speak_fn=lambda t: spoken.append(t))
    assert res.ok
    assert "2 agents on the board." in res.message
    assert "nova, claude, working: fixing the parser." in res.message
    assert "atlas, claude, needs input: approve deploy?" in res.message
    assert spoken == [res.message]  # the digest is exactly what gets spoken


def test_execute_read_board_excludes_the_board_pane_and_unnamed():
    # The board pane (find_board_pane) and unnamed plain shells are not agents.
    panes = [_pane("%1", "nova", active=True, session="repo"),
             _pane("%9", "board", session="repo"),      # the board pane
             _pane("%3", "%3", session="repo")]         # unnamed shell
    reg = FakeRegistry(panes, focused=panes[0])
    seen = []
    execute_command(
        Command(kind="read", to_all=True), reg, Config(),
        io=FakeTmux(board_pane="%9"),
        statuses_fn=lambda p: seen.append([x.id for x in p]) or [],
        speak_fn=lambda t: None)
    assert seen == [["%1"]]  # only the named, non-board agent pane


def test_execute_read_board_scopes_to_focused_session():
    panes = [_pane("%1", "nova", active=True, session="repoA"),
             _pane("%2", "atlas", session="repoA"),
             _pane("%3", "orion", session="repoB")]
    reg = FakeRegistry(panes, focused=panes[0])
    seen = []
    execute_command(
        Command(kind="read", to_all=True), reg, Config(), io=FakeTmux(),
        statuses_fn=lambda p: seen.append({x.id for x in p}) or [],
        speak_fn=lambda t: None)
    assert seen == [{"%1", "%2"}]  # repoB's orion is excluded


def test_execute_read_board_failure_is_reported_not_raised():
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])

    def boom(_panes):
        raise RuntimeError("summarizer exploded")

    res = execute_command(
        Command(kind="read", to_all=True), reg, Config(), io=FakeTmux(),
        statuses_fn=boom, speak_fn=lambda t: None)
    assert not res.ok and "couldn't read the board" in res.message


def test_execute_read_board_no_agents_still_ok():
    reg = FakeRegistry([], focused=None)
    res = execute_command(
        Command(kind="read", to_all=True), reg, Config(), io=FakeTmux(),
        statuses_fn=lambda p: [], speak_fn=lambda t: None)
    assert res.ok and "No agents" in res.message


def test_parse_read_yields_to_configured_slash_verb():
    # A user who maps "read" to a slash command keeps it; built-in read yields,
    # so the read verb never silently shadows configured slash commands.
    cfg = Config(slash_commands={"read": "/read"})
    c = _parse_btn("read nova", cfg)
    assert c.kind == "slash" and c.text == "/read" and c.name == "nova"
    # ...but with the default config (no such slash), read still works.
    assert _parse_btn("read nova").kind == "read"


def test_execute_read_summarize_failure_is_reported_not_raised():
    # Read runs on a worker thread, so a raising summarizer must degrade to a
    # CommandResult, never propagate (which would kill the thread silently).
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])

    def boom(tail, _title):
        raise RuntimeError("summarizer exploded")

    res = execute_command(
        Command(kind="read", name="nova"), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "tail", title_fn=lambda pid: "", summarize_fn=boom,
        speak_fn=lambda t: None)
    assert not res.ok and "couldn't read nova" in res.message


def test_execute_read_speak_failure_is_swallowed_summary_survives():
    # TTS is best-effort: a raising speaker must not lose the summary, which
    # still surfaces on the status line via the CommandResult message.
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])

    def boom(text):
        raise RuntimeError("tts exploded")

    res = execute_command(
        Command(kind="read", name="nova"), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "tail", title_fn=lambda pid: "",
        summarize_fn=lambda tail, _title: _summary("all green"), speak_fn=boom)
    assert res.ok and res.message == "nova: all green"


def test_default_summarizer_threads_config_and_title(monkeypatch):
    # The read-back default uses the richer summarize_read, threading the board
    # summarizer command/timeout AND the pane title through to it.
    from vupai.commands import _default_summarizer
    calls = []
    monkeypatch.setattr(
        "vupai.summarize.summarize_read",
        lambda tail, *, cmd, timeout, title: calls.append((tail, cmd, timeout, title))
        or _summary("x"))
    cfg = Config(board_summarizer_cmd="codex exec", board_summary_timeout_s=12.0)
    _default_summarizer(cfg)("the tail", "Fix the parser")
    assert calls == [("the tail", "codex exec", 12.0, "Fix the parser")]


def test_execute_read_passes_pane_title_to_summarizer():
    # The pane title (what the pane is about) is fetched and handed to the
    # summarizer for context.
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])
    seen = {}

    def summarize_fn(tail, title):
        seen["title"] = title
        return _summary("done")

    res = execute_command(
        Command(kind="read", name="nova"), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "tail", title_fn=lambda pid: "Add voice read-back",
        summarize_fn=summarize_fn, speak_fn=lambda t: None)
    assert res.ok
    assert seen["title"] == "Add voice read-back"


def test_parse_talkback_mute_and_unmute_phrases():
    for phrase in ("mute", "quiet", "be quiet", "stop talking", "shut up",
                   "the mute", "mute please"):
        c = _parse_btn(phrase)
        assert c is not None and c.kind == "talkback" and c.enable is False, phrase
    for phrase in ("unmute", "speak up", "talk back", "talk to me", "start talking"):
        c = _parse_btn(phrase)
        assert c is not None and c.kind == "talkback" and c.enable is True, phrase


def test_parse_talkback_only_on_button_key():
    # The mute/unmute words are common; they are commands only on the system key.
    # On the dictation/keyword key they fall through to verbatim injection.
    assert _parse("mute") is None
    assert _parse("talk back") is None


def test_parse_unrelated_phrase_is_not_talkback():
    # A near-miss must not toggle: it falls through (None -> dictation/route).
    assert _parse_btn("muted colors") is None
    assert _parse_btn("speak to nova") is None


def test_intent_phrase_is_present_tense_per_kind():
    from vupai.commands import intent_phrase
    assert intent_phrase(Command(kind="close", name="sage")) == "closing sage"
    assert intent_phrase(Command(kind="create", count=1)) == "opening an agent"
    assert intent_phrase(Command(kind="create", count=3)) == "opening 3 agents"
    assert intent_phrase(Command(kind="focus", name="nova")) == "switching to nova"
    assert intent_phrase(Command(kind="swap", name="a", name_b="b")) == "swapping a and b"
    assert intent_phrase(Command(kind="close_others")) == "closing the other agents"
    assert intent_phrase(Command(kind="slash", text="/clear")) == "sending clear"
    assert intent_phrase(Command(kind="zoom")) == "zooming"
    # read / talkback / unknown carry their own feedback -> no up-front intent.
    assert intent_phrase(Command(kind="read")) == ""
    assert intent_phrase(Command(kind="talkback", enable=True)) == ""


def test_execute_talkback_reports_state_and_speaks_only_on_unmute():
    on = execute_command(Command(kind="talkback", enable=True), FakeRegistry([]),
                         Config(), io=FakeTmux())
    assert on.ok and on.message == "talk-back on" and on.spoken == "talk back on"
    off = execute_command(Command(kind="talkback", enable=False), FakeRegistry([]),
                          Config(), io=FakeTmux())
    # Muted: no spoken twin (by the time it would speak, talk-back is already off).
    assert off.ok and "muted" in off.message and off.spoken == ""


def test_execute_create_has_say_friendly_spoken_ack(monkeypatch):
    monkeypatch.setattr("vupai.commands.shutil.which", lambda c: "/bin/claude")
    focused = _pane("%0", "%0", active=True)
    reg = FakeRegistry([focused], focused=focused)
    one = execute_command(Command(kind="create", count=1, unit="pane"), reg,
                          Config(), io=FakeTmux(new_ids=["%1"]))
    assert one.spoken == "nova is up"
    reg = FakeRegistry([focused], focused=focused)
    two = execute_command(Command(kind="create", count=2, unit="pane"), reg,
                          Config(), io=FakeTmux(new_ids=["%1", "%2"]))
    assert two.spoken == "2 agents up: nova, atlas"


def test_execute_swap_and_broadcast_spoken_drops_symbols():
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])
    swap = execute_command(Command(kind="swap", name="nova", name_b="atlas"), reg,
                           Config(), io=FakeTmux())
    assert "<->" in swap.message and swap.spoken == "swapped nova and atlas"
    bc = execute_command(Command(kind="broadcast", text="go"), reg, Config(),
                         io=FakeTmux(), inject_fn=lambda *a, **k: True)
    assert "/" in bc.message and bc.spoken == "broadcast to 2 of 2 agents"


def test_execute_slash_spoken_strips_leading_slash():
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])
    res = execute_command(Command(kind="slash", text="/clear", name="nova"), reg,
                          Config(), io=FakeTmux(), inject_fn=lambda *a, **k: True)
    assert res.message == "sent /clear to nova" and res.spoken == "sent clear to nova"


def test_bound_tail_byte_limit_keeps_end_and_is_utf8_safe():
    from vupai.commands import _READ_TAIL_BYTES, _bound_tail
    # Few lines (under the line cap) but far over the byte cap: the byte branch
    # must trim from the front, keep the end, and never split a multi-byte char.
    text = "head\n" + "x" * (_READ_TAIL_BYTES * 2) + "😀tail"
    bounded = _bound_tail(text)
    raw = bounded.encode("utf-8")
    assert len(raw) <= _READ_TAIL_BYTES
    assert bounded.endswith("😀tail")
    assert "�" not in bounded  # no half-decoded byte from a mid-char cut


# --- read command: streaming path -------------------------------------------

def test_execute_read_streams_sentences_with_label_on_first():
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])
    spoken = []

    def stream_fn(tail, title, on_text):
        on_text("Tests pass. ")        # one complete sentence
        on_text("Build is green.")     # held until close (no trailing space)
        return _summary("Tests pass. Build is green.")

    res = execute_command(
        Command(kind="read", name=""), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "tail", title_fn=lambda pid: "",
        stream_fn=stream_fn, speak_fn=lambda t: spoken.append(t))
    assert res.ok
    assert res.message == "nova: Tests pass. Build is green."
    # The callsign rides into the first spoken sentence; the rest follows in order.
    assert spoken == ["nova: Tests pass.", "Build is green."]


def test_execute_read_streaming_unnamed_pane_has_no_prefix():
    focused = _pane("%5", "%5", active=True)  # unnamed: name == id
    reg = FakeRegistry([focused], focused=focused)
    spoken = []

    def stream_fn(tail, title, on_text):
        on_text("Idle shell. ")
        return _summary("Idle shell.")

    res = execute_command(
        Command(kind="read", name=""), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "tail", title_fn=lambda pid: "",
        stream_fn=stream_fn, speak_fn=lambda t: spoken.append(t))
    assert res.ok and res.message == "Idle shell."
    assert spoken == ["Idle shell."]  # no "<label>: " prefix


def test_execute_read_streaming_failure_is_reported_not_raised():
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])

    def boom(tail, title, on_text):
        raise RuntimeError("summarizer died mid-stream")

    res = execute_command(
        Command(kind="read", name=""), reg, Config(), io=FakeTmux(),
        capture_fn=lambda pid: "tail", title_fn=lambda pid: "",
        stream_fn=boom, speak_fn=lambda t: None)
    assert not res.ok
    assert "couldn't read nova" in res.message


def test_execute_read_streaming_disabled_uses_oneshot_summarizer():
    # tts_stream off -> the injected one-shot summarize_fn drives, not stream_fn.
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])
    cfg = Config(tts_stream=False)
    spoken, streamed = [], []
    res = execute_command(
        Command(kind="read", name=""), reg, cfg, io=FakeTmux(),
        capture_fn=lambda pid: "tail", title_fn=lambda pid: "",
        summarize_fn=lambda tail, _t: _summary("one shot summary"),
        stream_fn=lambda *a: streamed.append(a),
        speak_fn=lambda t: spoken.append(t))
    assert res.ok and res.message == "nova: one shot summary"
    assert spoken == ["nova: one shot summary"]
    assert streamed == []  # stream_fn never consulted


# --- read board: streaming path ---------------------------------------------

def test_execute_read_board_streaming_speaks_header_then_each_agent(monkeypatch):
    import vupai.commands as cmds
    from vupai.board import PaneStatus
    from vupai.panestate import PaneState
    panes = [_pane("%1", "nova", active=True), _pane("%2", "atlas")]
    reg = FakeRegistry(panes, focused=panes[0])
    sts = [PaneStatus("nova", "claude", PaneState.WORKING, "fixing the parser", False),
           PaneStatus("atlas", "claude", PaneState.NEEDS_INPUT, "approve deploy?", True)]

    def fake_collect(panes_arg, *, summarize_fn, on_status=None, **kw):
        out = []
        for s in sts:  # simulate summaries landing in pane order
            if on_status is not None:
                on_status(s)
            out.append(s)
        return out

    monkeypatch.setattr(cmds.board, "collect_statuses", fake_collect)
    spoken = []
    res = execute_command(
        Command(kind="read", to_all=True), reg, Config(), io=FakeTmux(),
        speak_fn=lambda t: spoken.append(t))
    assert res.ok
    assert "2 agents on the board." in res.message
    # Header is voiced first, then each agent clause in order, as they land.
    assert spoken == [
        "2 agents on the board.",
        "nova, claude, working: fixing the parser.",
        "atlas, claude, needs input: approve deploy?",
    ]


def test_execute_read_board_streaming_no_agents_is_reported():
    focused = _pane("%1", "%1", active=True)  # unnamed: not an agent
    reg = FakeRegistry([focused], focused=focused)
    spoken = []
    res = execute_command(
        Command(kind="read", to_all=True), reg, Config(), io=FakeTmux(),
        speak_fn=lambda t: spoken.append(t))
    assert res.ok and res.message == "No agents to report."
    assert spoken == ["No agents to report."]


def test_execute_read_board_streaming_failure_is_reported_not_raised(monkeypatch):
    import vupai.commands as cmds
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])

    def boom(*a, **k):
        raise RuntimeError("collect exploded")

    monkeypatch.setattr(cmds.board, "collect_statuses", boom)
    res = execute_command(
        Command(kind="read", to_all=True), reg, Config(), io=FakeTmux(),
        speak_fn=lambda t: None)
    assert not res.ok and "couldn't read the board" in res.message


# --- read stream: cancel + cap ----------------------------------------------

def test_exec_read_stream_applies_cap_and_cancel():
    # Without cancel, a cap of 2 stops speaking after two sentences.
    cfg = Config(tts_stream=True, read_max_sentences=2)

    def fake_stream_fn(tail, title, on_text):
        for chunk in ["A. ", "B. ", "C. ", "D. "]:
            on_text(chunk)
        from vupai.summarize import Summary
        return Summary("A. B. C. D.", False, "llm")

    spoken = []
    panes = [_pane("%1", "nova", active=True)]
    reg = FakeRegistry(panes, focused=panes[0])

    res = execute_command(
        Command(kind="read"), reg, cfg, io=FakeTmux(),
        capture_fn=lambda _pid: "output",
        title_fn=lambda _pid: "title",
        speak_fn=lambda text: spoken.append(text),
        stream_fn=fake_stream_fn)

    assert res.ok
    # label "nova: " rides into the first sentence; cap=2 stops after two.
    assert spoken == ["nova: A.", "B."]


# ---------------------------------------------------------------------------
# Task 6: transient stop command
# ---------------------------------------------------------------------------

def _parse_stop_helper(text):
    return parse_command(
        text, broadcast_word="all", macros={}, programs={},
        slash_commands={}, addressing="button")


@pytest.mark.parametrize("phrase", [
    "stop", "enough", "that's enough", "thats enough", "cancel",
    "never mind", "nevermind", "skip", "stop it", "that's all", "thats all",
])
def test_stop_words_parse_to_stop(phrase):
    cmd = _parse_stop_helper(phrase)
    assert cmd is not None and cmd.kind == "stop"


@pytest.mark.parametrize("phrase", ["mute", "quiet", "hush", "stop talking", "stop reading"])
def test_mute_words_still_parse_to_talkback_off(phrase):
    cmd = _parse_stop_helper(phrase)
    assert cmd is not None and cmd.kind == "talkback" and cmd.enable is False


def test_execute_stop_returns_stopped():
    from vupai.commands import Command, execute_command
    from vupai.config import Config
    res = execute_command(Command(kind="stop"), registry=None, config=Config())
    assert res.ok and res.message == "stopped"


# ---------------------------------------------------------------------------
# Task 3: ssh/connect command parsing
# ---------------------------------------------------------------------------


def test_parse_ssh_basic():
    cmd = _parse_ssh(["ssh", "vm1"])
    assert cmd is not None and cmd.kind == "ssh" and cmd.name == "vm1"


def test_parse_ssh_connect_to():
    cmd = _parse_ssh(["connect", "to", "gpu", "box"])
    assert cmd is not None and cmd.kind == "ssh" and cmd.name == "gpu box"


def test_parse_ssh_connect_without_to():
    cmd = _parse_ssh(["connect", "staging"])
    assert cmd is not None and cmd.name == "staging"


def test_parse_ssh_no_phrase_returns_none():
    assert _parse_ssh(["ssh"]) is None
    assert _parse_ssh(["connect", "to"]) is None


def test_parse_ssh_non_verb_returns_none():
    assert _parse_ssh(["focus", "nova"]) is None
