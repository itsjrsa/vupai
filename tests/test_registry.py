
from vupai.registry import Pane, PaneRegistry, parse_panes

# Two windows. Window @0 ("main") has a named pane and an unnamed pane;
# window @1 ("editor") has one active named pane. Fields are tab-separated
# in PANE_FORMAT order:
#   pane_id  window_id  window_name  pane_index  @vupai_name  pane_current_command
#   pane_active  session_name
SAMPLE = [
    "%0\t@0\tmain\t0\tbackend\tclaude\t1\trepo",
    "%1\t@0\tmain\t1\t\tzsh\t0\trepo",      # unnamed pane: @vupai_name unset (empty field)
    "%2\t@1\teditor\t0\tnotes\tnvim\t1\trepo",
]


def test_parse_panes_fields():
    panes = parse_panes(SAMPLE)
    assert len(panes) == 3

    p0 = panes[0]
    assert p0 == Pane(
        id="%0",
        window_id="@0",
        window="main",
        index=0,
        name="backend",
        command="claude",
        active=True,
        session="repo",
    )

    # index is parsed to int, active "0" -> False
    assert panes[1].index == 1
    assert panes[1].active is False
    assert panes[1].name == "%1"

    assert panes[2].window == "editor"
    assert panes[2].active is True


def test_parse_panes_skips_blank_lines():
    lines = ["", "%0\t@0\tmain\t0\tbackend\tclaude\t1\trepo", "   "]
    panes = parse_panes(lines)
    assert len(panes) == 1
    assert panes[0].id == "%0"


def test_refresh_populates_from_lister():
    reg = PaneRegistry(lister=lambda: list(SAMPLE), focuser=lambda: None)
    assert reg.panes == []          # nothing loaded before refresh
    reg.refresh()
    assert len(reg.panes) == 3
    assert reg.panes[0].id == "%0"


def test_refresh_replaces_previous_panes():
    state = {"lines": list(SAMPLE)}
    reg = PaneRegistry(lister=lambda: state["lines"], focuser=lambda: None)
    reg.refresh()
    assert len(reg.panes) == 3
    state["lines"] = ["%9\t@2\tnew\t0\tfresh\tbash\t1\trepo"]
    reg.refresh()
    assert len(reg.panes) == 1
    assert reg.panes[0].id == "%9"


def test_focused_returns_matching_pane():
    reg = PaneRegistry(lister=lambda: list(SAMPLE), focuser=lambda: "%2")
    reg.refresh()
    f = reg.focused()
    assert f is not None
    assert f.id == "%2"
    assert f.name == "notes"


def test_focused_none_when_no_server_or_no_match():
    reg = PaneRegistry(lister=lambda: list(SAMPLE), focuser=lambda: None)
    reg.refresh()
    assert reg.focused() is None

    reg2 = PaneRegistry(lister=lambda: list(SAMPLE), focuser=lambda: "%999")
    reg2.refresh()
    assert reg2.focused() is None


def test_get_exact_case_insensitive():
    reg = PaneRegistry(lister=lambda: list(SAMPLE), focuser=lambda: None)
    reg.refresh()
    assert reg.get("backend").id == "%0"
    assert reg.get("BACKEND").id == "%0"
    assert reg.get("Backend").id == "%0"


def test_get_miss_returns_none():
    reg = PaneRegistry(lister=lambda: list(SAMPLE), focuser=lambda: None)
    reg.refresh()
    assert reg.get("nope") is None
