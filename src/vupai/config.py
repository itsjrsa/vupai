"""Configuration model and loader for vupai."""

from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    hotkey: str = "alt_r"                 # pynput Key name; alt_r = Right-Option
    addressing: str = "button"            # "button" (two-key, default) | "keyword"
    command_hotkey: str = "cmd_r"         # button mode: the system key (Right-Command)
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
    # Pause between the pasted text being confirmed in the pane and the Enter
    # that submits it, so you can read it and cancel a mishearing by clearing the
    # input (Esc / Ctrl-U) during the window. Applies to spoken dictation/
    # name-routed text only, not slash/broadcast. Set 0.0 to submit immediately;
    # a longer value also stalls the next utterance by that much.
    inject_submit_delay: float = 1.5
    aliases: dict[str, str] = field(default_factory=dict)  # spoken alias -> pane name
    broadcast_word: str = "everyone"      # leading word = inject to all agents
    pane_command: str = "claude"          # default program for created panes
    programs: dict[str, str] = field(     # spoken token -> argv ("" = default shell)
        default_factory=lambda: {
            "claude": "claude", "codex": "codex", "shell": "",
            "opencode": "opencode", "pi": "pi"})
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
    # Rotating example-command tips in tmux's status-left (a discoverability aid
    # for the voice grammar). Set false to leave status-left untouched.
    status_tips: bool = True
    status_tips_interval: float = 15.0  # seconds between tip rotations
    # Require confirmation before a destructive command (close / close others /
    # broadcast) fires. On by default: ASR mishears verbs (the alias tables
    # include real words), so a misheard destructive action should not act on a
    # single transcript. A tmux popup asks y/n; anything but yes (or a
    # confirm_timeout_s lapse) cancels - fail-safe. Set false to disable.
    confirm_destructive: bool = True
    confirm_timeout_s: float = 8.0
    # Confirm before a create command opens many panes at once. A large fan-out
    # tiles the window tight and (past ~16 names) makes voice addressing
    # unreliable, so a create with count >= this threshold gets the same y/n
    # popup as destructive commands. Shares the confirm_destructive master switch
    # and confirm_timeout_s. Set high (e.g. 99) to effectively never prompt.
    confirm_create_threshold: int = 8
    # Live transcript HUD: echo what was heard (and surface rejections) on the
    # target pane via tmux display-message, so a misroute/mishearing is visible
    # where you're looking. Set false to leave the status segment as the only
    # surface. Verbatim dictation is never echoed (the text lands in the pane).
    hud_enabled: bool = True
    # Agent-state poller (see watcher.py): watch named panes and fire a macOS
    # notification when an agent goes busy -> idle (finished). OFF by default -
    # it adds a background thread and the busy/idle heuristic is unvalidated on a
    # live Claude TUI; enable once tuned. notify_poll_interval is the tick cadence
    # (s); notify_capture_lines is how much of each pane's tail to classify.
    notify_enabled: bool = False
    notify_poll_interval: float = 2.0
    notify_capture_lines: int = 12
    # Strip non-lexical filler tokens (um, uh, er, ah, eh, hmm, mm) from every
    # transcript before commands/routing/dictation see it. On by default: the
    # default set is non-lexical only, so removal is essentially risk-free, and
    # the effect is visible in the journal (filtered_transcript). Add soft
    # fillers (like, so, you know) at your own risk; none ship by default.
    filler_filter: bool = True
    filler_words: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"um", "uh", "er", "ah", "eh", "hmm", "mm"}))


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
    # TOML has no set type: accept filler_words as a list and normalize to a
    # lowercased frozenset matching the field type.
    if "filler_words" in kwargs:
        kwargs["filler_words"] = frozenset(
            str(w).lower() for w in kwargs["filler_words"])
    return Config(**kwargs)


_STARTER_HEADER = (
    "# vupai config - see Config in src/vupai/config.py for every key."
)


ANNOTATED_TEMPLATE = '''\
# vupai config - every available key, defaulted and commented out.
# Uncomment a line (drop the leading "# ") and edit its value to override.
# See Config in src/vupai/config.py for the authoritative defaults.
# A running daemon loads config once at spawn: `vupai reload` to apply changes.

# pynput Key name for the push-to-talk dictation key. alt_r = Right-Option.
# hotkey = "alt_r"
# Addressing mode: "button" (two-key default) or "keyword" (legacy single key,
# no command layer).
# addressing = "button"
# button mode only: the system/command key that runs the command layer.
# command_hotkey = "cmd_r"
# ASR model id. English-only; the v3 multilingual model drifts to Russian on
# short audio.
# model_id = "mlx-community/parakeet-tdt-0.6b-v2"
# Capture sample rate (Hz).
# sample_rate = 16000
# CoreAudio input device name (sox AUDIODEV). "" = macOS system default.
# Set via `vupai mic`; resolved at daemon startup with fallback to default.
# mic_device = ""
# rapidfuzz name-match score, 0..100. Higher = stricter.
# fuzzy_cutoff = 82
# tmux pane-registry refresh cadence (seconds).
# poll_interval = 0.5
# Seconds to wait for pasted text to appear in the pane before giving up.
# inject_confirm_timeout = 2.0
# Poll cadence (seconds) while waiting for the paste to confirm.
# inject_poll_interval = 0.05
# Pause (seconds) between confirmed paste and the Enter that submits it, so a
# mishearing can be cancelled. Applies to dictation/name-routed text only.
# Set 0.0 to submit immediately.
# inject_submit_delay = 1.5
# Spoken alias -> pane name overrides for routing.
# [aliases]
# "nova" = "atlas"
# Leading spoken word that injects to all named agents.
# broadcast_word = "everyone"
# Default program launched in a newly created pane ("" = plain shell).
# pane_command = "claude"
# Spoken token -> argv for `create` ("" = default shell).
# [programs]
# claude = "claude"
# codex = "codex"
# shell = ""
# opencode = "opencode"
# pi = "pi"
# Spoken phrase -> ordered list of actions (macro).
# [macros]
# "set up" = ["create two panes", "tile"]
# Spoken verb -> literal slash string injected into the target pane(s).
# [slash_commands]
# clear = "/clear"
# compact = "/compact"
# Utterance journal: a JSONL trail (transcript + decision + outcome) at
# ~/.config/vupai/journal.jsonl, for diagnosing misfires.
# journal_enabled = true
# Opt-in: also retain each wav (your voice) for offline misfire replay.
# journal_keep_audio = false
# Ring bound: how many wavs to keep when journal_keep_audio is on.
# journal_audio_retention = 500
# Render an ambient daemon-state segment in tmux status-right.
# status_indicator = true
# Rotating example-command tips in tmux status-left (voice-grammar
# discoverability aid). Set false to leave status-left untouched.
# status_tips = true
# Seconds between status-left tip rotations.
# status_tips_interval = 15.0
# Require y/n confirmation before a destructive command (close / broadcast).
# confirm_destructive = true
# Seconds before the confirm popup auto-cancels (fail-safe).
# confirm_timeout_s = 8.0
# Confirm before a create opens at least this many panes at once.
# confirm_create_threshold = 8
# Live transcript HUD: echo what was heard on the target pane.
# hud_enabled = true
# Agent-state poller: notify when an agent goes busy -> idle. Off by default
# (background thread; busy/idle heuristic unvalidated on a live Claude TUI).
# notify_enabled = false
# Poller tick cadence (seconds).
# notify_poll_interval = 2.0
# How many lines of each pane's tail to classify busy/idle.
# notify_capture_lines = 12
# Strip non-lexical filler tokens before commands/routing/dictation.
# filler_filter = true
# The filler set (non-lexical only by default; add soft fillers at your risk).
# filler_words = ["um", "uh", "er", "ah", "eh", "hmm", "mm"]
'''


def render_config(active: dict[str, str]) -> str:
    """Return ANNOTATED_TEMPLATE with the named scalar keys uncommented.

    `active` maps a scalar config key to its already-TOML-formatted RHS string
    (e.g. "true", '"alt_r"'). Each matching `# key = ...` line becomes
    `key = <value>`; keys absent from `active` stay commented. Commented
    `[table]` blocks and array fields are never altered.
    """
    if not active:
        return ANNOTATED_TEMPLATE
    matchers = {
        key: re.compile(rf"^#\s*{re.escape(key)}\s*=") for key in active
    }
    out: list[str] = []
    done: set[str] = set()
    for line in ANNOTATED_TEMPLATE.splitlines():
        for key, matcher in matchers.items():
            if key not in done and matcher.match(line):
                out.append(f"{key} = {active[key]}")
                done.add(key)
                break
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def write_full_config(
    *, journal_enabled: bool, journal_keep_audio: bool,
    path: Path | None = None,
) -> Path:
    """Write a fresh full annotated config.toml.

    Every key is present and commented at its default; the two journal toggles
    are written uncommented to the given values. Intended for the first-run
    `setup` prompt (it does NOT merge into an existing file). Drop-in
    replacement for the old write_journal_config.
    """
    target = path if path is not None else CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    active = {
        "journal_enabled": str(journal_enabled).lower(),
        "journal_keep_audio": str(journal_keep_audio).lower(),
    }
    target.write_text(render_config(active), encoding="utf-8")
    return target


def regenerate_config(*, path: Path | None = None) -> tuple[Path, Path | None]:
    """(Re)write the all-commented annotated template, backing up any existing
    file to <path>.bak first. Returns (written_path, backup_path_or_None)."""
    target = path if path is not None else CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        shutil.copyfile(target, backup)
    target.write_text(render_config({}), encoding="utf-8")
    return target, backup


def _escape_toml(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _merge_scalar_keys(updates: dict[str, str], *, path: Path | None) -> Path:
    """Merge `key = "value"` string assignments into config.toml in place.

    Replaces each existing assignment (preserving comments and every other key)
    or appends it if absent, creating a starter file when none exists. `updates`
    maps config key name -> raw string value; values are TOML-escaped here.
    """
    target = path if path is not None else CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    new_lines = {
        key: f'{key} = "{_escape_toml(val)}"' for key, val in updates.items()
    }
    matchers = {
        key: re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=") for key in updates
    }

    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()
    else:
        lines = [_STARTER_HEADER]

    out: list[str] = []
    replaced: set[str] = set()
    for line in lines:
        for key, matcher in matchers.items():
            if key not in replaced and matcher.match(line):
                out.append(new_lines[key])
                replaced.add(key)
                break
        else:
            out.append(line)
    for key in updates:
        if key not in replaced:
            out.append(new_lines[key])
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    return target


def set_mic_device(name: str, *, path: Path | None = None) -> Path:
    """Persist the input-device selection into config.toml.

    Unlike `write_journal_config`, this MERGES into an existing file: it
    replaces an existing `mic_device = ...` assignment in place (preserving
    comments and every other key) or appends one if absent, creating a starter
    file when none exists. An empty `name` clears the pin (system default).
    """
    return _merge_scalar_keys({"mic_device": name}, path=path)


def set_hotkey_config(
    *, addressing: str, hotkey: str, command_hotkey: str,
    path: Path | None = None,
) -> Path:
    """Persist the trigger-key selection (addressing mode + PTT keys).

    Merges `addressing`, `hotkey`, and `command_hotkey` into config.toml in
    place (preserving comments and every other key), creating a starter file
    when none exists. Mirrors `set_mic_device`; written by `vupai keys` and the
    `vupai setup` hotkey step.
    """
    return _merge_scalar_keys(
        {
            "addressing": addressing,
            "hotkey": hotkey,
            "command_hotkey": command_hotkey,
        },
        path=path,
    )
