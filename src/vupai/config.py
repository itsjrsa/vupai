"""Configuration model and loader for vupai."""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

# Default board/read summarizer: the bundled streaming claude wrapper, invoked on
# THIS interpreter (sys.executable, which has vupai importable) the same way the
# daemon re-invokes itself (`-m vupai ...`). Streams Haiku token-by-token so the
# "read" talk-back speaks as it generates; swap for plain `claude -p`, an Ollama
# adapter, `codex exec`, etc. to opt out. Computed (not a literal) for the abs
# interpreter path, so it is never persisted into config.toml.
_DEFAULT_SUMMARIZER = f"{sys.executable} -m vupai.claude_summarize --model claude-haiku-4-5"


def _as_key_tuple(value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Normalize a hotkey field to a deduped tuple of pynput key names.

    Accepts a single string (the historical form) or a list/tuple of strings
    (the multi-key form). Order is preserved; duplicates and blanks are dropped.
    """
    names = [value] if isinstance(value, str) else list(value)
    out: list[str] = []
    for name in names:
        name = str(name).strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


@dataclass(frozen=True)
class Config:
    # pynput Key name(s) for the push-to-talk dictation key. A bare string or a
    # list of alternatives; normalized to a deduped tuple. alt_r = Right-Option.
    hotkey: str | list[str] | tuple[str, ...] = ("alt_r",)
    addressing: str = "button"            # "button" (two-key, default) | "keyword"
    # button mode: the system/command key(s). Same str-or-list shape as hotkey.
    command_hotkey: str | list[str] | tuple[str, ...] = ("cmd_r",)
    # English-only; v3 multilingual drifts to Russian on short audio.
    model_id: str = "mlx-community/parakeet-tdt-0.6b-v2"
    sample_rate: int = 16000
    # CoreAudio input device name (sox AUDIODEV). "" = macOS system default.
    # Set via `vupai mic`; resolved at daemon startup with fallback to default
    # if the device is absent (see audio.resolve_device).
    mic_device: str = ""
    fuzzy_cutoff: int = 82                 # rapidfuzz score 0..100
    # Lower cutoff for the destructive `close` command only, so a trailing-syllable
    # mishearing ("close novel" -> nova, ~67) still resolves. Kept below
    # fuzzy_cutoff: the curated callsign pool is mutually distinct down to this
    # value, so a looser bar here cannot confuse two open panes for each other.
    close_fuzzy_cutoff: int = 65
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
    # summary when the command is absent or fails. The default is the bundled
    # streaming Haiku wrapper (_DEFAULT_SUMMARIZER: `python -m vupai.claude_
    # summarize`), so "read" talk-back speaks token-by-token; plain `claude -p`
    # buffers and speaks once. To offload onto a (remote) Ollama box and skip the
    # ~3s `claude -p` CLI cold-start per call, point it at scripts/ollama_
    # summarize.py (keeps the model warm via keep_alive=-1); see that file's
    # header and board_summarizer_cmd below.
    board_enabled: bool = False
    board_summarizer_cmd: str = field(default_factory=lambda: _DEFAULT_SUMMARIZER)
    board_poll_interval: float = 2.0
    board_min_summary_interval: float = 30.0
    board_summary_timeout_s: float = 20.0
    # Talk-back (see speech.py): vupai speaks back a short spoken ack of every
    # command ("nova is up", "closed atlas") and reads pane summaries aloud for
    # "read <name>". tts_cmd is swappable (any TTS CLI that takes the phrase as its
    # last argument); the default is macOS `say`. tts_enabled is the persisted
    # master switch and the startup default for the runtime "mute"/"unmute" voice
    # command; it gates only the audio - with it off, acks and "read" still surface
    # on the status line, just silently.
    tts_enabled: bool = True
    tts_cmd: str = "say"
    # Stream the "read" summary: speak each sentence as the summarizer produces it
    # (first words out in ~1-2s) instead of waiting for the whole reply. Pays off
    # with a streaming board_summarizer_cmd (the default vupai.claude_summarize,
    # or the Ollama adapter); a buffering command (plain `claude -p`) still works,
    # it just speaks once at the end. Off -> the original whole-reply-at-once path.
    tts_stream: bool = True
    read_max_sentences: int = 2
    # Strip non-lexical filler tokens (um, uh, er, ah, eh, hmm, mm) from every
    # transcript before commands/routing/dictation see it. On by default: the
    # default set is non-lexical only, so removal is essentially risk-free, and
    # the effect is visible in the journal (filtered_transcript). Add soft
    # fillers (like, so, you know) at your own risk; none ship by default.
    filler_filter: bool = True
    filler_words: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"um", "uh", "er", "ah", "eh", "hmm", "mm"}))
    # tmux server socket name for vupai's sessions. vupai runs on its OWN tmux
    # server (this socket, via `tmux -L <name>`) so it never mutates your default
    # tmux server's global options, hooks, key bindings, numbering, or sessions.
    # Set "" to share your default server (legacy behavior). Restricted to a safe
    # filename charset (letters, digits, dot, dash, underscore) so it round-trips
    # through tmux's run-shell command strings.
    tmux_socket: str = "vupai"

    def __post_init__(self) -> None:
        # Coerce hotkey fields to deduped tuples regardless of how the Config was
        # built (defaults, direct construction with a string/list, or load_config),
        # so every consumer sees the same tuple shape.
        object.__setattr__(self, "hotkey", _as_key_tuple(self.hotkey))
        object.__setattr__(
            self, "command_hotkey", _as_key_tuple(self.command_hotkey))


CONFIG_PATH = Path.home() / ".config" / "vupai" / "config.toml"


def _warn(message: str) -> None:
    """Surface a non-fatal config problem on stderr (daemon stderr -> DAEMON_LOG)."""
    print(f"vupai: config warning: {message}", file=sys.stderr)


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML; missing file or keys fall back to defaults.

    Unknown keys are ignored but warned about: a scalar placed after a `[table]`
    header silently becomes a nested key (e.g. a top-level `confirm_destructive`
    appended below `[programs]` parses as `programs.confirm_destructive` and is
    lost). Warning on unknown top-level keys, and on non-string values inside the
    `[programs]` map (whose contract is token -> argv string), catches that.
    """
    target = path if path is not None else CONFIG_PATH
    if not target.exists():
        return Config()

    with target.open("rb") as fh:
        data = tomllib.load(fh)

    known = {f.name for f in fields(Config)}
    for key in data:
        if key not in known:
            _warn(f"unknown key '{key}' ignored "
                  "(misplaced under a [table] header, or a typo?)")
    programs = data.get("programs")
    if isinstance(programs, dict):
        for key, value in programs.items():
            if not isinstance(value, str):
                _warn(f"[programs] entry '{key}' = {value!r} is not a string; "
                      "a top-level key landing under [programs] is ignored - "
                      "move it above the [programs] header")
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
     '# pynput Key name(s) for the push-to-talk dictation key. alt_r = Right-Option.\n'
     '# A single key or a list of alternatives (any one triggers dictation), handy\n'
     '# when you swap between keyboards with different layouts.\n'
     '# hotkey = ["alt_r"]\n'),
    ("addressing",
     '# Addressing mode: "button" (two-key default) or "keyword" (legacy single key,\n'
     '# no command layer).\n'
     '# addressing = "button"\n'),
    ("command_hotkey",
     '# button mode only: the system/command key(s) that run the command layer.\n'
     '# A single key or a list of alternatives, same shape as hotkey.\n'
     '# command_hotkey = ["cmd_r"]\n'),
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
    ("close_fuzzy_cutoff",
     '# Looser name-match score for the destructive `close` command only, so a\n'
     '# trailing-syllable mishearing still resolves. Keep below fuzzy_cutoff.\n'
     '# close_fuzzy_cutoff = 65\n'),
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
     '# The pane tail rides as the final argument; the last non-blank stdout line\n'
     '# is the summary. Any CLI that reads a prompt as its last arg and prints one\n'
     '# line works, so this is also where you pick the model (it is whatever the\n'
     '# command uses). Degrades to a non-LLM last-line summary if absent or it\n'
     '# fails. Examples (uncomment one):\n'
     '#\n'
     '#   Default: bundled streaming Haiku wrapper, so "read" talk-back speaks\n'
     '#   token-by-token. Change the model via its --model flag.\n'
     '# board_summarizer_cmd = "python -m vupai.claude_summarize --model claude-haiku-4-5"\n'
     '#   Plain Claude (buffers, no streaming):\n'
     '# board_summarizer_cmd = "claude -p --model claude-haiku-4-5"\n'
     '#   Codex (model set via your Codex config/profile):\n'
     '# board_summarizer_cmd = "codex exec"\n'
     '#   Gemini CLI:\n'
     '# board_summarizer_cmd = "gemini -p"\n'
     '#   Local Ollama:\n'
     '# board_summarizer_cmd = "ollama run llama3.2"\n'
     '#   Remote Ollama (model on another host, skips the CLI cold-start):\n'
     '# board_summarizer_cmd = "python scripts/ollama_summarize.py --host http://BOX:11434 --model llama3.2"\n'),
    ("board_poll_interval",
     '# Board tick cadence (seconds).\n'
     '# board_poll_interval = 2.0\n'),
    ("board_min_summary_interval",
     '# Per-pane floor (seconds) between summaries; bounds worst-case spend.\n'
     '# board_min_summary_interval = 30.0\n'),
    ("board_summary_timeout_s",
     '# Hard timeout (seconds) for one summarizer invocation before falling back.\n'
     '# (`claude -p` cold-starts a CLI per call, and a remote Ollama pays a model\n'
     '# load on its first call after eviction, so keep this generous.)\n'
     '# board_summary_timeout_s = 20.0\n'),
    ("tts_enabled",
     '# Speak the "read <name>" command\'s summary aloud. On by default (the\n'
     '# command is explicit and macOS `say` ships with the OS). Turn off to keep\n'
     '# "read" silent - it still prints the summary to the status line.\n'
     '# tts_enabled = true\n'),
    ("tts_cmd",
     '# Text-to-speech command for "read"; the phrase rides as the final\n'
     '# argument. Swappable for any TTS CLI ("say -v Daniel", a neural-TTS\n'
     '# binary, "espeak"). Failures degrade silently to the on-screen summary.\n'
     '# tts_cmd = "say"\n'),
    ("tts_stream",
     '# Speak the "read" summary sentence-by-sentence as it is generated (first\n'
     '# words out in ~1-2s) instead of after the whole reply. Pays off with the\n'
     '# default streaming summarizer (or the Ollama adapter); a buffering command\n'
     '# (plain claude -p) still works, it just speaks once at the end.\n'
     '# tts_stream = true\n'),
    ("read_max_sentences",
     "# read_max_sentences: how many sentences a spoken `read` of a single pane\n"
     "# speaks before it stops (the streamed talk-back length cap). Lower = terser.\n"
     "# Does not apply to `read board`, whose length tracks the agent count.\n"
     '# read_max_sentences = 2\n'),
    ("filler_filter",
     '# Strip non-lexical filler tokens before commands/routing/dictation.\n'
     '# filler_filter = true\n'),
    ("filler_words",
     '# The filler set (non-lexical only by default; add soft fillers at your risk).\n'
     '# filler_words = ["um", "uh", "er", "ah", "eh", "hmm", "mm"]\n'),
    ("tmux_socket",
     "# tmux server socket for vupai's sessions, isolated from your default tmux\n"
     '# server so vupai never changes your existing tmux config/sessions. Set ""\n'
     '# to share the default server (legacy). Allowed: letters, digits, dot, dash,\n'
     '# underscore.\n'
     '# tmux_socket = "vupai"\n'),
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


def _format_toml(value: str | list[str] | tuple[str, ...]) -> str:
    """Render a config value as a TOML right-hand side.

    A string becomes a quoted scalar; a list/tuple becomes an inline array of
    quoted strings. Every element is TOML-escaped.
    """
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(f'"{_escape_toml(str(v))}"' for v in value) + "]"
    return f'"{_escape_toml(str(value))}"'


def _merge_scalar_keys(
    updates: dict[str, str | list[str] | tuple[str, ...]], *,
    path: Path | None,
) -> Path:
    """Merge `key = value` assignments into config.toml in place.

    Replaces each existing assignment (preserving comments and every other key)
    or appends it if absent, creating a starter file when none exists. `updates`
    maps config key name -> a string (quoted scalar) or a list/tuple (TOML
    array); values are formatted and escaped here.
    """
    target = path if path is not None else CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    new_lines = {
        key: f"{key} = {_format_toml(val)}" for key, val in updates.items()
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
    *, addressing: str,
    hotkey: str | list[str] | tuple[str, ...],
    command_hotkey: str | list[str] | tuple[str, ...],
    path: Path | None = None,
) -> Path:
    """Persist the trigger-key selection (addressing mode + PTT keys).

    Merges `addressing`, `hotkey`, and `command_hotkey` into config.toml in
    place (preserving comments and every other key), creating a starter file
    when none exists. `hotkey`/`command_hotkey` accept a single key name or a
    list of alternatives; lists are written as TOML arrays. Mirrors
    `set_mic_device`; written by `vupai keys` and the `vupai setup` hotkey step.
    """
    return _merge_scalar_keys(
        {
            "addressing": addressing,
            "hotkey": hotkey,
            "command_hotkey": command_hotkey,
        },
        path=path,
    )
