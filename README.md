# vupai

> **Voice UI for AI panes** — push-to-talk voice control for your tmux agent
> panes, on macOS, fully local.

*vupai* (say "voo-pie") is a **V**oice **U**I for your AI **pa**nes — hold a key,
speak, and what you say lands in the right one.

Hold a key, speak, and what you say is typed into the right tmux pane: the one
you're looking at, or an agent you call by name (*"nova, run the tests"*).
Speech-to-text runs on-device with NVIDIA Parakeet (via Apple MLX) — no cloud,
no API keys.

Built for a tmux-centric workflow where you keep several coding agents (Claude
Code) and shells open at once and want to drive them by voice without reaching
for the mouse.

## How it works

```
hold Right-Option → record (sox) → transcribe (Parakeet) → route → paste into a tmux pane → Enter
```

- **Routing is hybrid.** By default your speech goes to the **focused** pane. If
  you start with an agent's **name**, it goes there instead — even when it isn't
  focused. Say a **number** (*"two, …"*) to hit a pane by its position in the
  current window.
- **Injection is safe.** vupai pastes your text and waits until it actually
  appears in the pane before pressing Enter — it never blindly submits.
- **Local & private.** The model runs on your Mac; nothing leaves the machine.

## Requirements

vupai is **macOS Apple-Silicon only** — it depends on Apple MLX for on-device
speech, plus two Homebrew binaries. It will not run on Linux or Intel Macs.

- macOS on **Apple Silicon** (M-series), macOS 13.5+ (developed on macOS 26).
- [`tmux`](https://github.com/tmux/tmux) and [`sox`](https://sox.sourceforge.net/):
  ```bash
  brew install tmux sox
  ```
- Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).

## Install

After the Homebrew step above, install the `vupai` CLI in its own isolated
environment with [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install vupai                              # from PyPI
# or, to track the latest commit:
uv tool install git+https://github.com/itsjrsa/vupai
```

This puts `vupai` on your `PATH`. (`pipx install vupai` works the same way.)
The Parakeet model (~0.6B, ~2 GB) downloads automatically on first transcription.

To upgrade later: `uv tool upgrade vupai`.

### From source (development / dogfooding)

```bash
git clone git@github.com:itsjrsa/vupai.git
cd vupai
uv sync            # creates .venv and installs everything (incl. the MLX runtime)
```

Run the CLI with `uv run vupai …` from the repo, or see the dogfooding loop
(`vupai reload` / `vupai --reload`) in [AGENTS.md](AGENTS.md).

Working on vupai with an AI coding agent (Claude Code, Codex, opencode, Cursor,
Aider, …)? [AGENTS.md](AGENTS.md) is the single source of truth for repo
conventions, architecture, and invariants; [CLAUDE.md](CLAUDE.md) just points to
it.

## Grant macOS permissions (once)

vupai needs three permissions, granted to **your terminal app**
(Ghostty / iTerm / Terminal / …), under **System Settings → Privacy & Security**:
**Accessibility**, **Input Monitoring**, and **Microphone**. Run:

```bash
uv run vupai doctor
```

It probes each one and prints the exact System-Settings path for anything
missing. (macOS grants these to the terminal binary, not the script — so they
silently fail until granted.)

## Usage

```bash
uv run vupai            # ensures tmux + the voice daemon, then attaches you
```

`vupai` starts the push-to-talk daemon as a **detached background process**
(not a tmux window — it must run under your terminal app to receive global key
events) and attaches you to the tmux session. The daemon survives detach/reattach;
see its status with `vupai status`.

Then:

1. **Panes name themselves.** Every pane you create gets an auto-assigned callsign
   (the daemon installs tmux hooks for this), so you can address it by voice right
   away. To rename one, focus it and run `vupai name nova` (or target it:
   `vupai name nova %3`), or press **`<prefix>` + R** to rename the active pane.
2. **Hold Right-Option, speak, release.** What you said is typed into the target
   pane and submitted.

Examples (Right-Option held while speaking):

- *"run the tests"* → the **focused** pane.
- *"nova, deploy to staging"* → the pane named **nova**, wherever it is.
- *"two, git status"* → pane **2** in the current window.

If two names are too close to tell apart, vupai won't guess — it shows the
candidates so you can re-say.

### Voice commands

Beyond dictation, vupai has a small command layer. Hold the **system key** (the
`command_hotkey`, Right-Command by default) and speak; vupai executes the command
instead of typing it into a pane. The key is the signal, so there is no spoken
control word. Run `vupai voice-commands` for a cheat sheet tailored to your config.

- *"create 3 panes"* → spin up 3 auto-named panes, tiled (add a program:
  *"…create 2 shell panes"*). The noun is **optional** — *"create two"* or
  *"create a"* works — and *"agent(s)"* / *"split(s)"* are synonyms for *"pane(s)"*
  if "pane" gets misheard.
- *"focus nova"* → focus the **nova** pane (also: *"switch to / go to …"*).
- *"swap nova and atlas"* → swap two named panes.
- *"close nova"* → close a pane.
- *"clear"* / *"clear nova"* / *"clear all"* → send a **slash command** (`/clear`)
  to the focused pane, a named pane, or every named agent. Extend the spoken verbs
  via `slash_commands` in the config.
- *"everyone, pull main"* → broadcast the message to **every named agent**.
- A non-command on the system key (e.g. *"nova, run the tests"*) falls through to
  name addressing, so the same key both commands and addresses agents.
- Define your own **macros** (phrase → list of actions) in the config.

## Commands

| Command | What it does |
|---|---|
| `vupai` | Ensure tmux + the voice daemon, then attach (default) |
| `vupai --reload` | Respawn the daemon (pick up source edits), then attach — `reload && vupai` in one step |
| `vupai up` | Start the daemon without attaching |
| `vupai down` | Stop the daemon |
| `vupai reload` | Restart the daemon so source edits take effect (`down` + `up`) |
| `vupai name <name> [pane]` | Label a pane (defaults to focused; rejects confusable names) |
| `vupai autoname [pane]` | Assign the next free callsign to a pane (idempotent; used by the auto-name hooks) |
| `vupai status` | Show panes, daemon status, and permission state |
| `vupai mic [index\|name]` | List input devices, or pin one for speech (`vupai mic default` to unpin); `reload` to apply |
| `vupai voice-commands` | Print the spoken-command cheat sheet for your config |
| `vupai doctor` | Check permissions and print fix steps |

The push-to-talk daemon runs as a **detached background process** under your
terminal app (not inside tmux — that's required for the global hotkey to work).
It logs to `~/.config/vupai/daemon.log` and survives detach/reattach.

## Configuration

Optional TOML at `~/.config/vupai/config.toml` (every field has a default):

```toml
hotkey = "alt_r"                                  # pynput key name; alt_r = Right-Option (dictation key in button mode)
addressing = "button"                             # "button" (two keys, default) | "keyword" (one key, no command layer)
command_hotkey = "cmd_r"                          # button mode: the "system" key (Right-Command)
broadcast_word = "everyone"                       # leading word that injects to all named agents
model_id = "mlx-community/parakeet-tdt-0.6b-v2"   # English-only (v3 is multilingual and drifts to Russian on short clips)
sample_rate = 16000
mic_device = ""                                   # CoreAudio input name; "" = system default. Set via `vupai mic`
fuzzy_cutoff = 82                                 # name-match strictness (0-100)
poll_interval = 0.5                               # pane-registry refresh (s)
inject_confirm_timeout = 2.0                      # wait for pasted text before Enter (s)
inject_poll_interval = 0.05
pane_command = "claude"                           # default program for voice-created panes

[programs]                                        # spoken token -> argv ("" = plain shell)
claude = "claude"
shell = ""

[aliases]                                         # spoken alias -> pane name
# bot = "nova"

[macros]                                          # spoken phrase -> list of actions
# "start the squad" = ["create 3 panes", "tile"]

[slash_commands]                                  # spoken verb -> literal injected into the pane(s)
clear = "/clear"                                  # system key: "clear [name|all]"
compact = "/compact"
```

**Addressing modes.** In `button` mode (default) you hold one of two keys: the
dictation key (`hotkey`) types your words verbatim into the focused pane, while the
system key (`command_hotkey`) interprets them as a command, a broadcast, or a
name-addressed message ("nova, are you there?"). The key is the control signal, so
no spoken control word is needed. `keyword` mode is the legacy single-key mode: it
has no command layer - only the `broadcast_word` ("everyone ...") leads; everything
else is name-addressed or dictated verbatim to the focused pane.

## Scope & limitations

- **v1 targets Claude Code panes** (and plain shells). Codex/OpenCode have known
  TUI submit bugs and are out of scope for now.
- **Voice input only** — agents don't talk back (no TTS) yet.
- **Recognizer name-biasing is currently a no-op** — the installed `parakeet-mlx`
  doesn't accept hotwords, so vupai relies on fuzzy + phonetic matching of the
  spoken name instead (which handles most ASR slips). Pick distinctive,
  non-dictionary names for best results.

## Development

```bash
uv run pytest -m "not integration and not slow"   # fast unit suite (no tmux/mic/model)
uv run pytest -m integration                      # needs a real tmux
uv run pytest -m slow                             # needs the real model + tests/fixtures/tiny.wav
uv run ruff check .                               # lint
```

**Dogfooding tip:** the daemon loads vupai's modules once at spawn, so a live
one runs stale code after you edit the source. `uv run vupai --reload`
respawns it and re-attaches in one step. Install it as an editable tool to drop
the `uv run` prefix entirely:

```bash
uv tool install --editable .   # then just: vupai --reload
```

Architecture, module map, and the invariants to preserve are documented in
[`CLAUDE.md`](CLAUDE.md).

## License

[MIT](LICENSE). (Note: `pynput` is LGPL-3.0 and the Parakeet model weights are
CC-BY-4.0; both are runtime dependencies, not part of this repo's code.)
