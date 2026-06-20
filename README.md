# voxpane

> Push-to-talk voice control for your tmux agent panes — on macOS, fully local.

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
- **Injection is safe.** voxpane pastes your text and waits until it actually
  appears in the pane before pressing Enter — it never blindly submits.
- **Local & private.** The model runs on your Mac; nothing leaves the machine.

## Requirements

- macOS on **Apple Silicon** (M-series), macOS 13.5+ (developed on macOS 26).
- [`tmux`](https://github.com/tmux/tmux) and [`sox`](https://sox.sourceforge.net/):
  `brew install tmux sox`
- Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/) (recommended).

## Install

```bash
git clone git@github.com:itsjrsa/tmux-agents.git
cd tmux-agents
uv sync            # creates .venv and installs everything (incl. the MLX runtime)
```

The Parakeet model (~0.6B, ~2 GB) downloads automatically on first transcription.

## Grant macOS permissions (once)

voxpane needs three permissions, granted to **your terminal app**
(Ghostty / iTerm / Terminal / …), under **System Settings → Privacy & Security**:
**Accessibility**, **Input Monitoring**, and **Microphone**. Run:

```bash
uv run voxpane doctor
```

It probes each one and prints the exact System-Settings path for anything
missing. (macOS grants these to the terminal binary, not the script — so they
silently fail until granted.)

## Usage

```bash
uv run voxpane            # ensures tmux + the voice daemon, then attaches you
```

`voxpane` starts the push-to-talk daemon as a **detached background process**
(not a tmux window — it must run under your terminal app to receive global key
events) and attaches you to the tmux session. The daemon survives detach/reattach;
see its status with `voxpane status`.

Then:

1. **Panes name themselves.** Every pane you create gets an auto-assigned callsign
   (the daemon installs tmux hooks for this), so you can address it by voice right
   away. To rename one, focus it and run `voxpane name nova` (or target it:
   `voxpane name nova %3`), or press **`<prefix>` + R** to rename the active pane.
2. **Hold Right-Option, speak, release.** What you said is typed into the target
   pane and submitted.

Examples (Right-Option held while speaking):

- *"run the tests"* → the **focused** pane.
- *"nova, deploy to staging"* → the pane named **nova**, wherever it is.
- *"two, git status"* → pane **2** in the current window.

If two names are too close to tell apart, voxpane won't guess — it shows the
candidates so you can re-say.

### Voice commands

Beyond dictation, voxpane has a small command layer: prefix an utterance with the
**control word** (`computer` by default) and voxpane executes it instead of typing
it into a pane. Run `voxpane voice-commands` for a cheat sheet tailored to your
config.

- *"computer create 3 panes"* → spin up 3 auto-named panes, tiled (add a program:
  *"…create 2 shell panes"*).
- *"computer focus nova"* → focus the **nova** pane (also: *"switch to / go to …"*).
- *"computer swap nova and atlas"* → swap two named panes.
- *"computer close nova"* → close a pane.
- *"computer clear"* / *"computer clear nova"* / *"computer clear all"* → send a
  **slash command** (`/clear`) to the focused pane, a named pane, or every named
  agent. Extend the spoken verbs via `slash_commands` in the config.
- *"everyone, pull main"* → broadcast the message to **every named agent**.
- Define your own **macros** (phrase → list of actions) in the config.

## Commands

| Command | What it does |
|---|---|
| `voxpane` | Ensure tmux + the voice daemon, then attach (default) |
| `voxpane up` | Start the daemon without attaching |
| `voxpane down` | Stop the daemon |
| `voxpane reload` | Restart the daemon so source edits take effect (`down` + `up`) |
| `voxpane name <name> [pane]` | Label a pane (defaults to focused; rejects confusable names) |
| `voxpane autoname [pane]` | Assign the next free callsign to a pane (idempotent; used by the auto-name hooks) |
| `voxpane status` | Show panes, daemon status, and permission state |
| `voxpane voice-commands` | Print the spoken-command cheat sheet for your config |
| `voxpane doctor` | Check permissions and print fix steps |

The push-to-talk daemon runs as a **detached background process** under your
terminal app (not inside tmux — that's required for the global hotkey to work).
It logs to `~/.config/voxpane/daemon.log` and survives detach/reattach.

## Configuration

Optional TOML at `~/.config/voxpane/config.toml` (every field has a default):

```toml
hotkey = "alt_r"                                  # pynput key name; alt_r = Right-Option (dictation key in button mode)
addressing = "keyword"                            # "keyword" (one key + control word) | "button" (two keys)
command_hotkey = "ctrl_l"                         # button mode: the "system" key (Left-Control)
control_word = "computer"                         # keyword mode: leading word that addresses voxpane
broadcast_word = "everyone"                       # leading word that injects to all named agents
model_id = "mlx-community/parakeet-tdt-0.6b-v2"   # English-only (v3 is multilingual and drifts to Russian on short clips)
sample_rate = 16000
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
clear = "/clear"                                  # "computer clear [name|all]"
compact = "/compact"
```

**Addressing modes.** In `keyword` mode (default) you hold one key and select a
voxpane command by speaking the `control_word` ("computer ...") or broadcast with
the `broadcast_word` ("everyone ..."). In `button` mode you hold one of two keys:
the dictation key (`hotkey`) types your words verbatim into the focused pane,
while the system key (`command_hotkey`) interprets them as a command, a broadcast,
or a name-addressed message ("nova, are you there?"). The button is the control
signal, so no control word is needed.

## Scope & limitations

- **v1 targets Claude Code panes** (and plain shells). Codex/OpenCode have known
  TUI submit bugs and are out of scope for now.
- **Voice input only** — agents don't talk back (no TTS) yet.
- **Recognizer name-biasing is currently a no-op** — the installed `parakeet-mlx`
  doesn't accept hotwords, so voxpane relies on fuzzy + phonetic matching of the
  spoken name instead (which handles most ASR slips). Pick distinctive,
  non-dictionary names for best results.

## Development

```bash
uv run pytest -m "not integration and not slow"   # fast unit suite (no tmux/mic/model)
uv run pytest -m integration                      # needs a real tmux
uv run pytest -m slow                             # needs the real model + tests/fixtures/tiny.wav
uv run ruff check .                               # lint
```

Architecture, module map, and the invariants to preserve are documented in
[`CLAUDE.md`](CLAUDE.md).

## License

Not yet licensed — the code is currently "all rights reserved" by default until
a license is chosen. (Note: `pynput` is LGPL-3.0 and the Parakeet model weights
are CC-BY-4.0; both are runtime dependencies, not part of this repo's code.)
