"""Configuration model and loader for vtmux."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    hotkey: str = "alt_r"                 # pynput Key name; alt_r = Right-Option
    model_id: str = "mlx-community/parakeet-tdt-0.6b-v3"
    sample_rate: int = 16000
    fuzzy_cutoff: int = 82                 # rapidfuzz score 0..100
    poll_interval: float = 0.5             # registry refresh cadence (s)
    inject_confirm_timeout: float = 2.0    # s to wait for pasted text to appear
    inject_poll_interval: float = 0.05
    voice_window_name: str = "voice"
    aliases: dict[str, str] = field(default_factory=dict)  # spoken alias -> pane name


CONFIG_PATH = Path.home() / ".config" / "vtmux" / "config.toml"


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
