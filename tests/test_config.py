import re
import tomllib
from dataclasses import fields
from pathlib import Path

from vupai.config import (
    ANNOTATED_TEMPLATE,
    Config,
    load_config,
    render_config,
    set_hotkey_config,
    set_mic_device,
    update_config,
    write_full_config,
)


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does_not_exist.toml")
    assert isinstance(cfg, Config)
    assert cfg.hotkey == ("alt_r",)
    assert cfg.model_id == "mlx-community/parakeet-tdt-0.6b-v2"
    assert cfg.sample_rate == 16000
    assert cfg.fuzzy_cutoff == 82
    assert cfg.poll_interval == 0.5
    assert cfg.inject_confirm_timeout == 2.0
    assert cfg.inject_poll_interval == 0.05
    assert cfg.aliases == {}


def test_default_when_path_is_none(monkeypatch, tmp_path: Path) -> None:
    # path=None falls back to CONFIG_PATH; point that at a missing file.
    monkeypatch.setattr(
        "vupai.config.CONFIG_PATH", tmp_path / "missing" / "config.toml"
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
    assert cfg.hotkey == ("ctrl_r",)
    assert cfg.fuzzy_cutoff == 90
    assert cfg.poll_interval == 1.5
    assert cfg.aliases == {"claude": "main", "cc": "main"}
    # untouched fields keep defaults
    assert cfg.model_id == "mlx-community/parakeet-tdt-0.6b-v2"
    assert cfg.sample_rate == 16000


def test_unknown_keys_ignored(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        'hotkey = "alt_l"\n'
        'bogus_key = "ignored"\n'
        "another_unknown = 123\n"
    )
    cfg = load_config(p)
    assert cfg.hotkey == ("alt_l",)
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


def test_hotkey_defaults_are_tuples() -> None:
    c = Config()
    assert c.hotkey == ("alt_r",)
    assert c.command_hotkey == ("cmd_r",)


def test_hotkey_string_normalized_to_tuple() -> None:
    c = Config(hotkey="ctrl_r", command_hotkey="cmd")
    assert c.hotkey == ("ctrl_r",)
    assert c.command_hotkey == ("cmd",)


def test_hotkey_list_normalized_to_tuple() -> None:
    c = Config(hotkey=["alt_r", "f13"], command_hotkey=["cmd_r", "f14"])
    assert c.hotkey == ("alt_r", "f13")
    assert c.command_hotkey == ("cmd_r", "f14")


def test_hotkey_dedup_preserves_order() -> None:
    c = Config(hotkey=["alt_r", "f13", "alt_r"])
    assert c.hotkey == ("alt_r", "f13")


def test_load_config_hotkey_array(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('hotkey = ["alt_r", "f13"]\ncommand_hotkey = ["cmd_r"]\n')
    c = load_config(p)
    assert c.hotkey == ("alt_r", "f13")
    assert c.command_hotkey == ("cmd_r",)


def test_load_config_hotkey_scalar_still_works(tmp_path: Path) -> None:
    # Backward compatibility: a pre-existing scalar config keeps loading.
    p = tmp_path / "config.toml"
    p.write_text('hotkey = "ctrl_r"\n')
    c = load_config(p)
    assert c.hotkey == ("ctrl_r",)


def test_command_defaults() -> None:
    c = Config()
    assert c.broadcast_word == "everyone"
    assert c.pane_command == "claude"
    assert c.programs == {
        "claude": "claude", "codex": "codex", "shell": "",
        "opencode": "opencode", "pi": "pi"}
    assert c.macros == {}


def test_loads_command_config(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        'broadcast_word = "team"\n'
        'pane_command = "claude"\n\n'
        "[programs]\n"
        'claude = "claude"\n'
        'shell = ""\n\n'
        "[macros]\n"
        '"dev layout" = ["create 3 claude panes", "tile"]\n'
    )
    c = load_config(p)
    assert c.broadcast_word == "team"
    assert c.macros["dev layout"] == ["create 3 claude panes", "tile"]
    assert c.programs["shell"] == ""


def test_slash_commands_default() -> None:
    c = Config()
    assert c.slash_commands == {"clear": "/clear", "compact": "/compact"}


def test_loads_slash_commands(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        "[slash_commands]\n"
        'clear = "/clear"\n'
        'wipe = "/clear"\n'
    )
    c = load_config(p)
    assert c.slash_commands["wipe"] == "/clear"
    assert c.slash_commands["clear"] == "/clear"


def test_journal_defaults() -> None:
    c = Config()
    assert c.journal_enabled is True
    assert c.journal_keep_audio is False
    assert c.journal_audio_retention == 500


def test_write_full_config_roundtrips(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "config.toml"  # parent created on write
    out = write_full_config(
        journal_enabled=False, journal_keep_audio=True, path=p
    )
    assert out == p
    c = load_config(p)
    assert c.journal_enabled is False
    assert c.journal_keep_audio is True
    # full surface present: an unrelated key is in the file (commented)
    assert "# hotkey = " in p.read_text(encoding="utf-8")
    # untouched keys keep defaults
    assert c.journal_audio_retention == 500
    assert c.hotkey == ("alt_r",)


def test_update_config_creates_full_file_when_absent(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "config.toml"  # parent created on write
    written, added, created = update_config(path=p)
    assert written == p
    assert created is True
    # every field reported as added; file is the full annotated template
    assert "hotkey" in added and "filler_words" in added
    assert load_config(p) == Config()


def test_update_config_appends_only_missing_keys(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    # a hand-edited file: one active key, one chosen comment, no other keys
    p.write_text(
        "# my notes\nhotkey = \"f13\"\n", encoding="utf-8")
    written, added, created = update_config(path=p)
    assert written == p
    assert created is False
    # the present key is NOT re-added; everything else is
    assert "hotkey" not in added
    assert "journal_enabled" in added and "filler_words" in added
    text = p.read_text(encoding="utf-8")
    # original content preserved verbatim
    assert "# my notes\nhotkey = \"f13\"\n" in text
    # appended keys are commented defaults under a labeled separator
    assert "# --- keys added by `vupai config --init` ---" in text
    # the chosen value survives; newly added keys load at their defaults
    c = load_config(p)
    assert c.hotkey == ("f13",)
    assert c.journal_enabled is True
    assert c.confirm_create_threshold == 8


def test_update_config_noop_when_complete(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    # a file that already lists every key (the full template)
    write_full_config(journal_enabled=True, journal_keep_audio=False, path=p)
    before = p.read_text(encoding="utf-8")
    written, added, created = update_config(path=p)
    assert written == p
    assert created is False
    assert added == []
    # nothing appended: file is byte-for-byte unchanged
    assert p.read_text(encoding="utf-8") == before


def test_update_config_does_not_readd_a_substring_key(tmp_path: Path) -> None:
    # status_tips present must not mark status_tips_interval present (or vice
    # versa): the right-boundary in _field_present disambiguates.
    p = tmp_path / "config.toml"
    p.write_text("# status_tips = true\n", encoding="utf-8")
    _, added, _ = update_config(path=p)
    assert "status_tips" not in added
    assert "status_tips_interval" in added


def test_mic_device_default() -> None:
    assert Config().mic_device == ""


def test_set_mic_device_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "config.toml"
    out = set_mic_device("AirPods Pro", path=p)
    assert out == p
    assert load_config(p).mic_device == "AirPods Pro"


def test_set_mic_device_merges_preserving_other_keys(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    write_full_config(journal_enabled=False, journal_keep_audio=True, path=p)
    set_mic_device("USB Mic", path=p)
    c = load_config(p)
    assert c.mic_device == "USB Mic"
    # journal keys written earlier survive the merge
    assert c.journal_enabled is False
    assert c.journal_keep_audio is True
    # comments preserved
    assert "# vupai config" in p.read_text()


def test_set_mic_device_replaces_existing_value(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    set_mic_device("First", path=p)
    set_mic_device("Second", path=p)
    assert load_config(p).mic_device == "Second"
    # no duplicate assignment lines left behind
    assert p.read_text().count("mic_device =") == 1


def test_set_mic_device_empty_clears_pin(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    set_mic_device("Pinned", path=p)
    set_mic_device("", path=p)
    assert load_config(p).mic_device == ""


def test_set_mic_device_uncomments_in_place(tmp_path):
    # On a freshly generated all-commented file, setting mic must edit the
    # existing line, not append a second active duplicate below it.
    p = tmp_path / "config.toml"
    p.write_text(ANNOTATED_TEMPLATE, encoding="utf-8")
    set_mic_device("AirPods Pro", path=p)
    text = p.read_text(encoding="utf-8")
    assert text.count("mic_device =") == 1  # not duplicated
    assert 'mic_device = "AirPods Pro"' in text
    assert "# mic_device =" not in text     # the comment line was consumed
    assert load_config(p).mic_device == "AirPods Pro"
    # a different commented key is untouched
    assert "# hotkey = " in text


def test_addressing_defaults() -> None:
    c = Config()
    assert c.addressing == "button"
    assert c.command_hotkey == ("cmd_r",)


def test_loads_addressing_config(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('addressing = "button"\ncommand_hotkey = "ctrl_r"\n')
    c = load_config(p)
    assert c.addressing == "button"
    assert c.command_hotkey == ("ctrl_r",)


def test_set_hotkey_config_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "config.toml"
    out = set_hotkey_config(
        addressing="button", hotkey=["alt_r"], command_hotkey=["cmd"], path=p)
    assert out == p
    c = load_config(p)
    assert c.addressing == "button"
    assert c.hotkey == ("alt_r",)
    assert c.command_hotkey == ("cmd",)


def test_set_hotkey_config_writes_arrays(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    set_hotkey_config(
        addressing="button", hotkey=["alt_r", "f13"],
        command_hotkey=["cmd_r", "f14"], path=p)
    text = p.read_text()
    assert 'hotkey = ["alt_r", "f13"]' in text
    assert 'command_hotkey = ["cmd_r", "f14"]' in text
    c = load_config(p)
    assert c.hotkey == ("alt_r", "f13")
    assert c.command_hotkey == ("cmd_r", "f14")


def test_set_hotkey_config_merges_preserving_other_keys(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    set_mic_device("USB Mic", path=p)
    set_hotkey_config(
        addressing="button", hotkey=["f13"], command_hotkey=["cmd_r"], path=p)
    c = load_config(p)
    assert c.hotkey == ("f13",)
    assert c.command_hotkey == ("cmd_r",)
    # mic pin written earlier survives the merge
    assert c.mic_device == "USB Mic"
    assert "# vupai config" in p.read_text()


def test_set_hotkey_config_replaces_existing_values(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    set_hotkey_config(
        addressing="button", hotkey=["alt_r"], command_hotkey=["cmd_r"], path=p)
    set_hotkey_config(
        addressing="keyword", hotkey=["ctrl_r"], command_hotkey=["cmd_r"],
        path=p)
    c = load_config(p)
    assert c.addressing == "keyword"
    assert c.hotkey == ("ctrl_r",)
    text = p.read_text()
    assert text.count("hotkey =") == 2  # hotkey + command_hotkey, no dupes
    assert text.count("addressing =") == 1


# ---------------------------------------------------------------------------
# Gap 2: destructive-command confirmation config
# ---------------------------------------------------------------------------

def test_confirm_defaults():
    cfg = Config()
    assert cfg.confirm_destructive is True
    assert cfg.confirm_timeout_s == 8.0


def test_confirm_destructive_loadable_from_toml(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text("confirm_destructive = false\n")
    cfg = load_config(p)
    assert cfg.confirm_destructive is False


def test_hud_enabled_default_and_loadable(tmp_path: Path):
    assert Config().hud_enabled is True
    p = tmp_path / "config.toml"
    p.write_text("hud_enabled = false\n")
    assert load_config(p).hud_enabled is False


def test_notify_defaults_and_loadable(tmp_path: Path):
    assert Config().notify_enabled is False
    assert Config().notify_poll_interval == 2.0
    assert Config().notify_capture_lines == 12
    p = tmp_path / "config.toml"
    p.write_text("notify_enabled = true\nnotify_poll_interval = 1.5\n")
    cfg = load_config(p)
    assert cfg.notify_enabled is True
    assert cfg.notify_poll_interval == 1.5


def test_inject_submit_delay_default_and_loadable(tmp_path: Path):
    assert Config().inject_submit_delay == 1.5
    p = tmp_path / "config.toml"
    p.write_text("inject_submit_delay = 0.0\n")
    assert load_config(p).inject_submit_delay == 0.0


def test_filler_defaults():
    from vupai.config import Config
    cfg = Config()
    assert cfg.filler_filter is True
    assert cfg.filler_words == frozenset({"um", "uh", "er", "ah", "eh", "hmm", "mm"})


def test_filler_words_loaded_from_toml_as_frozenset(tmp_path):
    from vupai.config import load_config
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'filler_filter = false\nfiller_words = ["UM", "Like"]\n', encoding="utf-8"
    )
    cfg = load_config(cfg_file)
    assert cfg.filler_filter is False
    assert cfg.filler_words == frozenset({"um", "like"})


def test_status_tips_defaults_on():
    cfg = Config()
    assert cfg.status_tips is True
    assert cfg.status_tips_interval == 15.0


def test_status_tips_loads_from_toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("status_tips = false\nstatus_tips_interval = 30\n")
    cfg = load_config(p)
    assert cfg.status_tips is False
    assert cfg.status_tips_interval == 30


def test_template_is_valid_toml_and_all_defaults_when_commented(tmp_path):
    # Every line is commented, so parsing yields an empty doc and load_config
    # falls back to a pristine Config().
    p = tmp_path / "config.toml"
    p.write_text(ANNOTATED_TEMPLATE, encoding="utf-8")
    assert tomllib.loads(ANNOTATED_TEMPLATE) == {}
    assert load_config(p) == Config()


def test_template_covers_every_config_field():
    # Drift guard: a new Config field with no doc block + default fails here.
    for f in fields(Config):
        scalar = re.compile(rf"^#?\s*{re.escape(f.name)}\s*=", re.MULTILINE)
        table = re.compile(rf"^#?\s*\[{re.escape(f.name)}\]", re.MULTILINE)
        assert scalar.search(ANNOTATED_TEMPLATE) or table.search(
            ANNOTATED_TEMPLATE
        ), f"{f.name} missing from ANNOTATED_TEMPLATE"


def test_render_config_uncomments_named_scalar_keys(tmp_path):
    out = render_config(
        {"journal_enabled": "false", "mic_device": '"USB Mic"'}
    )
    p = tmp_path / "config.toml"
    p.write_text(out, encoding="utf-8")
    c = load_config(p)
    assert c.journal_enabled is False
    assert c.mic_device == "USB Mic"
    # untouched keys stay at defaults (still commented)
    assert c.journal_keep_audio is False
    assert c.hotkey == ("alt_r",)


def test_render_config_empty_active_equals_template():
    assert render_config({}) == ANNOTATED_TEMPLATE


def test_tts_defaults():
    cfg = Config()
    assert cfg.tts_enabled is True
    assert cfg.tts_cmd == "say"


def test_tts_overrides(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('tts_enabled = false\ntts_cmd = "say -v Daniel"\n')
    cfg = load_config(p)
    assert cfg.tts_enabled is False
    assert cfg.tts_cmd == "say -v Daniel"


def test_read_max_sentences_defaults_to_two():
    from vupai.config import Config
    assert Config().read_max_sentences == 2


def test_read_max_sentences_in_annotated_template():
    from vupai.config import ANNOTATED_TEMPLATE
    assert "read_max_sentences" in ANNOTATED_TEMPLATE
