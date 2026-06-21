"""Configuration model and loader for vupai."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    hotkey: str = "alt_r"                 # pynput Key name; alt_r = Right-Option
    addressing: str = "button"            # "button" (two-key, default) | "keyword"
    command_hotkey: str = "alt_l"         # button mode: the system key (Left-Option)
    # English-only; v3 multilingual drifts to Russian on short audio.
    model_id: str = "mlx-community/parakeet-tdt-0.6b-v2"
    sample_rate: int = 16000
    # CoreAudio input device name (sox AUDIODEV). "" = macOS system default.
    # Set via `vupai mic`; resolved at daemon startup with fallback to default
    # if the device is absent (see audio.resolve_device).
    mic_device: str = ""
    fuzzy_cutoff: int = 82                 # rapidfuzz score 0..100
    poll_interval: float = 0.5             # registry refresh cadence (s)
    inject_confirm_timeout: float = 2.0    # s to wait for pasted text to appear
    inject_poll_interval: float = 0.05
    aliases: dict[str, str] = field(default_factory=dict)  # spoken alias -> pane name
    broadcast_word: str = "everyone"      # leading word = inject to all agents
    pane_command: str = "claude"          # default program for created panes
    programs: dict[str, str] = field(     # spoken token -> argv ("" = default shell)
        default_factory=lambda: {"claude": "claude", "codex": "codex", "shell": ""})
    macros: dict[str, list[str]] = field(default_factory=dict)  # phrase -> actions
    # Spoken verb -> literal string injected into the target pane(s). Defaults are
    # fire-and-forget Claude Code slash commands; menu-opening ones (/model,
    # /agents) are deliberately omitted (they need follow-up keystrokes).
    slash_commands: dict[str, str] = field(
        default_factory=lambda: {"clear": "/clear", "compact": "/compact"})
    # Utterance journal: a JSONL trail of transcript + decision + outcome per
    # utterance, for reviewing/diagnosing misfires. On by default (transcripts
    # only). Set journal_enabled=false to record nothing. Audio is opt-in
    # (journal_keep_audio=true) and ring-bounded to journal_audio_retention wavs.
    journal_enabled: bool = True
    journal_keep_audio: bool = False
    journal_audio_retention: int = 500
    # Render an ambient daemon-state segment in tmux's status-right (listening /
    # working / last result / errors). Set false to leave status-right untouched.
    status_indicator: bool = True


CONFIG_PATH = Path.home() / ".config" / "vupai" / "config.toml"


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


def write_journal_config(
    *, enabled: bool, keep_audio: bool, path: Path | None = None
) -> Path:
    """Write a fresh config.toml carrying the journal toggles.

    Intended for the first-run `setup` prompt: it creates a starter file when
    none exists (it does NOT merge into an existing one). Only the journal keys
    are written; every other setting keeps its default via `load_config`.
    """
    target = path if path is not None else CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# vupai config - see Config in src/vupai/config.py for every key.\n"
        "\n"
        "# Utterance journal: a JSONL trail (transcript + decision + outcome)\n"
        "# at ~/.config/vupai/journal.jsonl, for diagnosing misfires.\n"
        f"journal_enabled = {str(enabled).lower()}\n"
        "# Opt-in: also retain each wav next to the journal (ring-bounded to\n"
        "# journal_audio_retention files) so a misfire can be replayed offline.\n"
        f"journal_keep_audio = {str(keep_audio).lower()}\n"
    )
    target.write_text(body, encoding="utf-8")
    return target


_MIC_LINE = re.compile(r"^\s*mic_device\s*=")


def set_mic_device(name: str, *, path: Path | None = None) -> Path:
    """Persist the input-device selection into config.toml.

    Unlike `write_journal_config`, this MERGES into an existing file: it
    replaces an existing `mic_device = ...` assignment in place (preserving
    comments and every other key) or appends one if absent, creating a starter
    file when none exists. An empty `name` clears the pin (system default).
    """
    target = path if path is not None else CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'mic_device = "{escaped}"'

    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()
    else:
        lines = [
            "# vupai config - see Config in src/vupai/config.py for every key."
        ]

    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and _MIC_LINE.match(line):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new_line)
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    return target
