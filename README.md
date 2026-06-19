# vtmux

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
- **Injection is safe.** vtmux pastes your text and waits until it actually
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

vtmux needs three permissions, granted to **your terminal app**
(Ghostty / iTerm / Terminal / …), under **System Settings → Privacy & Security**:
**Accessibility**, **Input Monitoring**, and **Microphone**. Run:

```bash
uv run vtmux doctor
```

It probes each one and prints the exact System-Settings path for anything
missing. (macOS grants these to the terminal binary, not the script — so they
silently fail until granted.)

## Usage

```bash
uv run vtmux            # boots tmux + a "voice" daemon window, then attaches you
```

Then:

1. **Name the panes** you want to address by voice: focus a pane and run
   `vtmux name nova` (or target one explicitly: `vtmux name nova %3`).
2. **Hold Right-Option, speak, release.** What you said is typed into the target
   pane and submitted.

Examples (Right-Option held while speaking):

- *"run the tests"* → the **focused** pane.
- *"nova, deploy to staging"* → the pane named **nova**, wherever it is.
- *"two, git status"* → pane **2** in the current window.

If two names are too close to tell apart, vtmux won't guess — it shows the
candidates so you can re-say.

## Commands

| Command | What it does |
|---|---|
| `vtmux` | Ensure tmux + the voice daemon, then attach (default) |
| `vtmux up` | Start the daemon without attaching |
| `vtmux down` | Stop the daemon and remove its voice window |
| `vtmux name <name> [pane]` | Label a pane (defaults to focused; rejects confusable names) |
| `vtmux status` | Show panes, daemon status, and permission state |
| `vtmux doctor` | Check permissions and print fix steps |

The push-to-talk daemon runs inside a dedicated **`voice`** tmux window so you
can see its status; it survives detach/reattach.

## Configuration

Optional TOML at `~/.config/vtmux/config.toml` (every field has a default):

```toml
hotkey = "alt_r"                                  # pynput key name; alt_r = Right-Option
model_id = "mlx-community/parakeet-tdt-0.6b-v3"
sample_rate = 16000
fuzzy_cutoff = 82                                 # name-match strictness (0–100)
poll_interval = 0.5                               # pane-registry refresh (s)
inject_confirm_timeout = 2.0                      # wait for pasted text before Enter (s)
inject_poll_interval = 0.05
voice_window_name = "voice"
```

## Scope & limitations

- **v1 targets Claude Code panes** (and plain shells). Codex/OpenCode have known
  TUI submit bugs and are out of scope for now.
- **Voice input only** — agents don't talk back (no TTS) yet.
- **Recognizer name-biasing is currently a no-op** — the installed `parakeet-mlx`
  doesn't accept hotwords, so vtmux relies on fuzzy + phonetic matching of the
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
