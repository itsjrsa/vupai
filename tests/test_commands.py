from voxpane.commands import parse_command
from voxpane.config import Config


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
