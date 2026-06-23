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
    # Supervision board (see board.py): a dedicated tmux pane that summarizes,
    # per agent pane, the main conclusion / pending action. Launch manually with
    # `vupai board`; board_enabled is reserved for a future auto-open on
    # `vupai up`. Summaries are edge-triggered (only when a pane settles), so
    # cost stays low. board_summarizer_cmd is swappable (e.g. "codex exec",
    # "gemini -p", "ollama run <model>") and degrades to a non-LLM last-line
    # summary when the command is absent or fails. The default uses Haiku, since
    # a one-line glance summary does not need a high-tier model.
    board_enabled: bool = False
    board_summarizer_cmd: str = "claude -p --model claude-haiku-4-5"
    board_poll_interval: float = 2.0
    board_min_summary_interval: float = 30.0
    board_summary_timeout_s: float = 20.0
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


_TEMPLATE_HEADER = (
    '# vupai config - every available key, defaulted and commented out.\n'
    '# Uncomment a line (drop the leading "# ") and edit its value to override.\n'
    '# See Config in src/vupai/config.py for the authoritative defaults.\n'
    '# A running daemon loads config once at spawn: `vupai reload` to apply changes.\n'
    '\n'
)

# (field_name, block) in Config declaration order. Each block is the field's doc
# comment line(s) plus its commented default - a scalar `# key = default`, or a
# commented `[table]` / array block for dict/set fields. This is the SINGLE
# SOURCE OF TRUTH for the generated file: ANNOTATED_TEMPLATE is built from it,
# the drift guard asserts every Config field has a block, and `update_config`
# appends only the blocks a file is missing. Adding a Config field => add a block.
_FIELD_BLOCKS: tuple[tuple[str, str], ...] = (
    ("hotkey",
     '# pynput Key name for the push-to-talk dictation key. alt_r = Right-Option.\n'
     '# hotkey = "alt_r"\n'),
    ("addressing",
     '# Addressing mode: "button" (two-key default) or "keyword" (legacy single key,\n'
     '# no command layer).\n'
     '# addressing = "button"\n'),
    ("command_hotkey",
     '# button mode only: the system/command key that runs the command layer.\n'
     '# command_hotkey = "cmd_r"\n'),
    ("model_id",
     '# ASR model id. English-only; the v3 multilingual model drifts to Russian on\n'
     '# short audio.\n'
     '# model_id = "mlx-community/parakeet-tdt-0.6b-v2"\n'),
    ("sample_rate",
     '# Capture sample rate (Hz).\n'
     '# sample_rate = 16000\n'),
    ("mic_device",
     '# CoreAudio input device name (sox AUDIODEV). "" = macOS system default.\n'
     '# Set via `vupai mic`; resolved at daemon startup with fallback to default.\n'
     '# mic_device = ""\n'),
    ("fuzzy_cutoff",
     '# rapidfuzz name-match score, 0..100. Higher = stricter.\n'
     '# fuzzy_cutoff = 82\n'),
    ("poll_interval",
     '# tmux pane-registry refresh cadence (seconds).\n'
     '# poll_interval = 0.5\n'),
    ("inject_confirm_timeout",
     '# Seconds to wait for pasted text to appear in the pane before giving up.\n'
     '# inject_confirm_timeout = 2.0\n'),
    ("inject_poll_interval",
     '# Poll cadence (seconds) while waiting for the paste to confirm.\n'
     '# inject_poll_interval = 0.05\n'),
    ("inject_submit_delay",
     '# Pause (seconds) between confirmed paste and the Enter that submits it, so a\n'
     '# mishearing can be cancelled. Applies to dictation/name-routed text only.\n'
     '# Set 0.0 to submit immediately.\n'
     '# inject_submit_delay = 1.5\n'),
    ("aliases",
     '# Spoken alias -> pane name overrides for routing.\n'
     '# [aliases]\n'
     '# "nova" = "atlas"\n'),
    ("broadcast_word",
     '# Leading spoken word that injects to all named agents.\n'
     '# broadcast_word = "everyone"\n'),
    ("pane_command",
     '# Default program launched in a newly created pane ("" = plain shell).\n'
     '# pane_command = "claude"\n'),
    ("programs",
     '# Spoken token -> argv for `create` ("" = default shell).\n'
     '# [programs]\n'
     '# claude = "claude"\n'
     '# codex = "codex"\n'
     '# shell = ""\n'
     '# opencode = "opencode"\n'
     '# pi = "pi"\n'),
    ("macros",
     '# Spoken phrase -> ordered list of actions (macro).\n'
     '# [macros]\n'
     '# "set up" = ["create two panes", "tile"]\n'),
    ("slash_commands",
     '# Spoken verb -> literal slash string injected into the target pane(s).\n'
     '# [slash_commands]\n'
     '# clear = "/clear"\n'
     '# compact = "/compact"\n'),
    ("journal_enabled",
     '# Utterance journal: a JSONL trail (transcript + decision + outcome) at\n'
     '# ~/.config/vupai/journal.jsonl, for diagnosing misfires.\n'
     '# journal_enabled = true\n'),
    ("journal_keep_audio",
     '# Opt-in: also retain each wav (your voice) for offline misfire replay.\n'
     '# journal_keep_audio = false\n'),
    ("journal_audio_retention",
     '# Ring bound: how many wavs to keep when journal_keep_audio is on.\n'
     '# journal_audio_retention = 500\n'),
    ("status_indicator",
     '# Render an ambient daemon-state segment in tmux status-right.\n'
     '# status_indicator = true\n'),
    ("status_tips",
     '# Rotating example-command tips in tmux status-left (voice-grammar\n'
     '# discoverability aid). Set false to leave status-left untouched.\n'
     '# status_tips = true\n'),
    ("status_tips_interval",
     '# Seconds between status-left tip rotations.\n'
     '# status_tips_interval = 15.0\n'),
    ("confirm_destructive",
     '# Require y/n confirmation before a destructive command (close / broadcast).\n'
     '# confirm_destructive = true\n'),
    ("confirm_timeout_s",
     '# Seconds before the confirm popup auto-cancels (fail-safe).\n'
     '# confirm_timeout_s = 8.0\n'),
    ("confirm_create_threshold",
     '# Confirm before a create opens at least this many panes at once.\n'
     '# confirm_create_threshold = 8\n'),
    ("hud_enabled",
     '# Live transcript HUD: echo what was heard on the target pane.\n'
     '# hud_enabled = true\n'),
    ("notify_enabled",
     '# Agent-state poller: notify when an agent goes busy -> idle. Off by default\n'
     '# (background thread; busy/idle heuristic unvalidated on a live Claude TUI).\n'
     '# notify_enabled = false\n'),
    ("notify_poll_interval",
     '# Poller tick cadence (seconds).\n'
     '# notify_poll_interval = 2.0\n'),
    ("notify_capture_lines",
     "# How many lines of each pane's tail to classify busy/idle.\n"
     '# notify_capture_lines = 12\n'),
    ("board_enabled",
     '# Supervision board: a dedicated tmux pane summarizing each agent pane.\n'
     '# Reserved for a future auto-open on `vupai up`; launch manually with\n'
     '# `vupai board` regardless.\n'
     '# board_enabled = false\n'),
    ("board_summarizer_cmd",
     '# Command that turns a pane\'s scrollback tail into a one-line summary.\n'
     '# Swappable: "codex exec", "gemini -p", "ollama run <model>", etc. The\n'
     '# prompt rides as the final argument; the last non-blank stdout line is the\n'
     '# summary. Degrades to a non-LLM last-line summary if absent or it fails.\n'
     '# board_summarizer_cmd = "claude -p --model claude-haiku-4-5"\n'),
    ("board_poll_interval",
     '# Board tick cadence (seconds).\n'
     '# board_poll_interval = 2.0\n'),
    ("board_min_summary_interval",
     '# Per-pane floor (seconds) between summaries; bounds worst-case spend.\n'
     '# board_min_summary_interval = 30.0\n'),
    ("board_summary_timeout_s",
     '# Hard timeout (seconds) for one summarizer invocation before falling back.\n'
     '# (`claude -p` cold-starts a CLI per call, so keep this generous.)\n'
     '# board_summary_timeout_s = 20.0\n'),
    ("filler_filter",
     '# Strip non-lexical filler tokens before commands/routing/dictation.\n'
     '# filler_filter = true\n'),
    ("filler_words",
     '# The filler set (non-lexical only by default; add soft fillers at your risk).\n'
     '# filler_words = ["um", "uh", "er", "ah", "eh", "hmm", "mm"]\n'),
)

ANNOTATED_TEMPLATE = _TEMPLATE_HEADER + "".join(
    block for _, block in _FIELD_BLOCKS)


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


def _field_present(text: str, name: str) -> bool:
    """Whether `name` already appears in config text as a key (active or
    commented), either a scalar `key =` or a `[table]` header. The `\\s*=` /
    `]` right-boundary stops a key matching a longer key that contains it
    (e.g. `poll_interval` will not match `notify_poll_interval`)."""
    scalar = re.compile(rf"^\s*#?\s*{re.escape(name)}\s*=", re.MULTILINE)
    table = re.compile(rf"^\s*#?\s*\[{re.escape(name)}\]", re.MULTILINE)
    return bool(scalar.search(text) or table.search(text))


def update_config(
    *, path: Path | None = None
) -> tuple[Path, list[str], bool]:
    """Ensure config.toml lists every Config key, appending ONLY the blocks it
    is missing (doc + commented default), never rewriting or reordering existing
    lines. Hand edits and any chosen values are preserved; nothing is backed up
    because nothing is overwritten. Backs the new keys with a labeled separator
    so a re-run after an upgrade just tops up the freshly added settings.

    Returns (path, added_keys, created). A missing file is created from the full
    annotated template (created=True, added_keys = every field). When the file
    already lists every key, added_keys is empty.
    """
    target = path if path is not None else CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(ANNOTATED_TEMPLATE, encoding="utf-8")
        return target, [name for name, _ in _FIELD_BLOCKS], True
    existing = target.read_text(encoding="utf-8")
    missing = [
        (name, block)
        for name, block in _FIELD_BLOCKS
        if not _field_present(existing, name)
    ]
    if not missing:
        return target, [], False
    sep = "" if existing.endswith("\n") else "\n"
    addition = "".join(block for _, block in missing)
    target.write_text(
        existing + sep
        + "\n# --- keys added by `vupai config --init` ---\n"
        + addition,
        encoding="utf-8",
    )
    return target, [name for name, _ in missing], False


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
