"""Configuration model and loader for voxpane."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    hotkey: str = "alt_r"                 # pynput Key name; alt_r = Right-Option
    addressing: str = "keyword"           # "keyword" | "button" (two-key mode)
    command_hotkey: str = "ctrl_l"        # button mode: the system key (Left-Control)
    # English-only; v3 multilingual drifts to Russian on short audio.
    model_id: str = "mlx-community/parakeet-tdt-0.6b-v2"
    sample_rate: int = 16000
    fuzzy_cutoff: int = 82                 # rapidfuzz score 0..100
    poll_interval: float = 0.5             # registry refresh cadence (s)
    inject_confirm_timeout: float = 2.0    # s to wait for pasted text to appear
    inject_poll_interval: float = 0.05
    aliases: dict[str, str] = field(default_factory=dict)  # spoken alias -> pane name
    control_word: str = "computer"        # leading word = a voxpane command
    broadcast_word: str = "everyone"      # leading word = inject to all agents
    pane_command: str = "claude"          # default program for created panes
    programs: dict[str, str] = field(     # spoken token -> argv ("" = default shell)
        default_factory=lambda: {"claude": "claude", "shell": ""})
    macros: dict[str, list[str]] = field(default_factory=dict)  # phrase -> actions
    # Spoken verb -> literal string injected into the target pane(s). Defaults are
    # fire-and-forget Claude Code slash commands; menu-opening ones (/model,
    # /agents) are deliberately omitted (they need follow-up keystrokes).
    slash_commands: dict[str, str] = field(
        default_factory=lambda: {"clear": "/clear", "compact": "/compact"})


CONFIG_PATH = Path.home() / ".config" / "voxpane" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML; missing file or keys fall back to defaults.

    Unknown keys in the file are ignored.
    """
    target = path if path is not None else CONFIG_PATH
    if not target.exists():
        return Config()

    with target.open("rb") as fh:
        data = tomllib.load(fh)

    known = {f.name for f in fields(Config)}
    kwargs = {key: value for key, value in data.items() if key in known}
    return Config(**kwargs)
