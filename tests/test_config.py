from pathlib import Path

from voxpane.config import Config, load_config


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does_not_exist.toml")
    assert isinstance(cfg, Config)
    assert cfg.hotkey == "alt_r"
    assert cfg.model_id == "mlx-community/parakeet-tdt-0.6b-v3"
    assert cfg.sample_rate == 16000
    assert cfg.fuzzy_cutoff == 82
    assert cfg.poll_interval == 0.5
    assert cfg.inject_confirm_timeout == 2.0
    assert cfg.inject_poll_interval == 0.05
    assert cfg.voice_window_name == "voice"
    assert cfg.aliases == {}


def test_default_when_path_is_none(monkeypatch, tmp_path: Path) -> None:
    # path=None falls back to CONFIG_PATH; point that at a missing file.
    monkeypatch.setattr(
        "voxpane.config.CONFIG_PATH", tmp_path / "missing" / "config.toml"
    )
    cfg = load_config(None)
    assert cfg == Config()


def test_overrides_selected_fields(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        'hotkey = "ctrl_r"\n'
        "fuzzy_cutoff = 90\n"
        "poll_interval = 1.5\n"
        "\n"
        "[aliases]\n"
        'claude = "main"\n'
        'cc = "main"\n'
    )
    cfg = load_config(p)
    assert cfg.hotkey == "ctrl_r"
    assert cfg.fuzzy_cutoff == 90
    assert cfg.poll_interval == 1.5
    assert cfg.aliases == {"claude": "main", "cc": "main"}
    # untouched fields keep defaults
    assert cfg.model_id == "mlx-community/parakeet-tdt-0.6b-v3"
    assert cfg.sample_rate == 16000


def test_unknown_keys_ignored(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        'hotkey = "alt_l"\n'
        'bogus_key = "ignored"\n'
        "another_unknown = 123\n"
    )
    cfg = load_config(p)
    assert cfg.hotkey == "alt_l"
    assert not hasattr(cfg, "bogus_key")
    assert cfg == Config(hotkey="alt_l")


def test_config_is_frozen() -> None:
    cfg = Config()
    try:
        cfg.hotkey = "ctrl_r"  # type: ignore[misc]
    except Exception as exc:  # frozen dataclass raises FrozenInstanceError
        assert "FrozenInstanceError" in type(exc).__name__
    else:
        raise AssertionError("Config should be frozen")


def test_command_defaults() -> None:
    c = Config()
    assert c.control_word == "computer"
    assert c.broadcast_word == "everyone"
    assert c.pane_command == "claude"
    assert c.programs == {"claude": "claude", "shell": ""}
    assert c.macros == {}


def test_loads_command_config(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        'control_word = "jarvis"\n'
        'broadcast_word = "team"\n'
        'pane_command = "claude"\n\n'
        "[programs]\n"
        'claude = "claude"\n'
        'shell = ""\n\n'
        "[macros]\n"
        '"dev layout" = ["create 3 claude panes", "tile"]\n'
    )
    c = load_config(p)
    assert c.control_word == "jarvis"
    assert c.broadcast_word == "team"
    assert c.macros["dev layout"] == ["create 3 claude panes", "tile"]
    assert c.programs["shell"] == ""


def test_addressing_defaults() -> None:
    c = Config()
    assert c.addressing == "keyword"
    assert c.command_hotkey == "ctrl_l"


def test_loads_addressing_config(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('addressing = "button"\ncommand_hotkey = "ctrl_r"\n')
    c = load_config(p)
    assert c.addressing == "button"
    assert c.command_hotkey == "ctrl_r"
