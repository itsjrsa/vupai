<h1 align="center">vupai</h1>

<p align="center">
  <strong>Voice UI for AI panes</strong>: push-to-talk voice control for your tmux agent panes, on macOS, fully local.
</p>

<p align="center">
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-blue.svg"></a>
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/python-%3E%3D3.11-brightgreen.svg"></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-black.svg">
</p>

*vupai* (say "voo-pie") is a **V**oice **U**I for your AI **pa**nes.

Hold a key, speak, and what you say is typed into the right tmux pane: the one
you're looking at, or an agent you call by name (*"atlas, run the tests"*).
Speech-to-text runs on-device with NVIDIA Parakeet (via Apple MLX): no cloud,
no API keys.

Built for a tmux-centric workflow where you keep several coding agents (Claude
Code) and shells open at once and want to drive them by voice without reaching
for the mouse.

## Why not plain tmux?

vupai *runs on* tmux: it doesn't replace it, it adds a voice layer on top. tmux
already gives you panes, splits, and a way to keep many agents on screen. What it
can't do is let you talk to them. That's the gap vupai fills.

| With plain tmux | With vupai |
|---|---|
| Switch panes with `<prefix>`-arrow, then type | **Hold a key and talk** to the focused pane |
| Manually track which pane is which agent | Panes **auto-name themselves**; address them by name (*"atlas, run the tests"*) |
| Re-type the same command in each pane | **Broadcast by voice** to every agent at once (*"everyone, pull main"*) |
| Split / resize / re-layout with prefix chords | **Voice commands**: *"create 3 panes"*, *"focus atlas"*, *"swap atlas and sage"*, *"tile"* |
| Read each pane yourself to see what agents are doing | **Supervision board** + *"read atlas"* speaks a one-line summary aloud |
| n/a | **On-device speech** (Parakeet via Apple MLX) - no cloud, no API keys |

If you only have one shell open, you don't need vupai. It earns its keep when you
are juggling several agents and want to drive them hands-on-keyboard-optional.

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
- **Fully local, fully private.** Speech-to-text runs entirely on-device via
  Apple MLX (NVIDIA Parakeet). There is no cloud service, no API key, and no
  account: your voice and transcripts never leave your Mac. The only network
  access is a one-time model download (~2 GB) on first use.

## Requirements

> [!IMPORTANT]
> vupai is **macOS Apple-Silicon only**: it depends on Apple MLX for on-device
> speech, plus two Homebrew binaries. It will not run on Linux or Intel Macs.
> On an unsupported host the CLI fails fast with a clear message instead of a
> stray import error, and `parakeet-mlx` is skipped at install time.

- macOS on **Apple Silicon** (M-series), macOS 13.5+ (developed on macOS 26).
- [`tmux`](https://github.com/tmux/tmux) and [`sox`](https://sox.sourceforge.net/):
  ```bash
  brew install tmux sox
  ```
- Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).

## Install

After the Homebrew step above, install the `vupai` CLI in its own isolated
environment straight from the repo with [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/itsjrsa/vupai
```

This puts `vupai` on your `PATH`. (`pipx install git+https://github.com/itsjrsa/vupai`
works the same way.) The Parakeet model (~0.6B, ~2 GB) downloads automatically on
first transcription.

To upgrade later: `uv tool upgrade vupai`. Not on PyPI yet; install from git or
[from source](#from-source-development--dogfooding).

### From source (development / dogfooding)

```bash
git clone git@github.com:itsjrsa/vupai.git
cd vupai
uv sync            # creates .venv and installs everything (incl. the MLX runtime)
```

Run the CLI with `uv run vupai …` from the repo, or see the dogfooding loop
(`vupai reload` / `vupai --reload`) in [AGENTS.md](AGENTS.md).

> [!NOTE]
> The examples below use the bare `vupai` command (the installed tool). If you're
> running from a source checkout, prefix each one with `uv run` (e.g. `uv run
> vupai setup`).

Working on vupai with an AI coding agent (Claude Code, Codex, opencode, Cursor,
Aider, …)? [AGENTS.md](AGENTS.md) is the single source of truth for repo
conventions, architecture, and invariants; [CLAUDE.md](CLAUDE.md) just points to
it.

## Set up (once)

The fastest path after install is the interactive bootstrap:

```bash
vupai setup
```

It walks you through everything first-run: checks the Homebrew tools, captures
journaling consent, lets you pick a mic and your push-to-talk key(s)/addressing
mode, downloads the speech model up front (so the first hotkey press doesn't
stall on a silent fetch), then deep-links you to each macOS permission pane that
still needs your terminal app enabled. It's safe to re-run any time.

### Grant macOS permissions

`setup` handles these, but to check them on their own: vupai needs three
permissions, granted to **your terminal app** (Ghostty / iTerm / Terminal / …),
under **System Settings → Privacy & Security**: **Accessibility**, **Input
Monitoring**, and **Microphone**. Run:

```bash
vupai doctor
```

It probes each one and prints the exact System-Settings path for anything
missing.

> [!WARNING]
> macOS grants these to the terminal binary, not the script, so the hotkey and
> mic silently fail until you grant them. If voice seems dead, this is the first
> thing to check (`vupai doctor`).

## Usage

Start vupai inside a project, open a few agent panes, and drive them by voice. The
push-to-talk daemon runs in the background, so you stay in tmux and just hold a key
to talk. Launch (or re-attach to) a session with:

```bash
vupai                 # attach-or-create the session named after the cwd
vupai attach backend  # attach to "backend" (create it if absent)
vupai new backend     # create "backend" (error if it already exists)
vupai kill backend    # kill the "backend" session
```

> [!NOTE]
> **vupai runs on its own tmux server**, so it never touches your existing tmux
> setup. The trade-off: its sessions don't show in a plain `tmux ls`; reach them
> with `vupai attach`. (Set `tmux_socket = ""` to share your default server.)

Once attached, you talk to vupai with **two push-to-talk keys**:

| Key | Config | Default | Hold and speak to… |
|---|---|---|---|
| **Dictation key** | `hotkey` | Right-Option | Type your words into the **focused** pane. |
| **System key** | `command_hotkey` | Right-Command | Run a **voice command** (below). The key is the signal, so there is no spoken control word; vupai acts on the panes instead of typing. |

Both defaults are customizable: set `hotkey` / `command_hotkey` in the config (each
takes a list, so you can bind several keys to one action).

### Voice commands

Hold the **system key** and say any of these. Run `vupai voice-commands` for a
cheat sheet tailored to your config.

| Say | What happens |
|---|---|
| *"create 3 panes"* | Spin up N auto-named panes, tiled (up to 30; *"create 2 shell panes"* picks the program) |
| *"focus atlas"* | Focus the **atlas** pane (also *"switch to / go to"*) |
| *"swap atlas and sage"* | Swap two named panes |
| *"zoom atlas"* / *"unzoom"* | Maximize a pane / restore the layout |
| *"tile"* / *"layout …"* | Re-layout the window (tiled, main-vertical, …) |
| *"close atlas"* / *"kill atlas"* | Close a pane (asks y/n by default) |
| *"board"* | Open the **supervision board** (one per session) |
| *"read atlas"* / *"read all"* | Speak a pane's summary aloud (`read board` for a digest) |
| *"clear atlas"* / *"clear all"* | Send a slash command (`/clear`) to a pane or every agent |
| *"everyone, pull main"* | **Broadcast** the message to every named agent |
| *"connect to box"* / *"ssh box"* | SSH the focused pane into a configured host |
| *"mute"* / *"unmute"* / *"stop"* | Silence/restore talk-back, or cut off the current read |
| *"atlas, run the tests"* | Not a command, so it falls through to **name addressing** |

## Commands

Run `vupai --help` for the full command list (and `vupai <command> --help` for a
specific one). The everyday ones are in [Usage](#usage) above; a few worth
knowing: `vupai setup` (first-run bootstrap), `vupai voice-commands` (spoken-command
cheat sheet for your config), and `vupai board` (the [supervision
board](#supervision-board)).

The push-to-talk daemon runs as a **detached background process** under your
terminal app (not inside tmux — that's required for the global hotkey to work).
It logs to `~/.config/vupai/daemon.log` and survives detach/reattach.

## Supervision board

When you have several agents running at once, you can't watch them all. The
**supervision board** does it for you: `vupai board` (or just say *"board"*)
splits a dedicated pane (right, ~40%) that shows, per named agent pane, a
one-line summary of its main conclusion or pending action — so a glance tells you
who's done, who's stuck, and who needs you.

- **Tool-agnostic.** Works with any agentic CLI, not just Claude Code: pane
  activity is detected from terminal-output churn, and the summarizer is a
  swappable command (`board_summarizer_cmd` — Haiku by default, or point it at
  codex/gemini/ollama).
- **Cheap by design.** A pane is summarized only when it *settles* (finishes a
  burst of work), skipped when nothing changed, and throttled per pane
  (`board_min_summary_interval`).
- **Speak it too.** *"read board"* reads the digest aloud; *"read atlas"* reads a
  single pane.

One board per session. Close the pane to stop it.

## Configuration

`vupai setup` creates `~/.config/vupai/config.toml` on first run, pre-filled with
every field at its default value (so the file below is what you start with); it's
left untouched if one already exists. Editing it is optional. You can also create
or top it up at any time with `vupai config --init`, which adds any keys a newer
version introduced without disturbing your edits.

```toml
hotkey = ["alt_r"]                                # pynput key name(s); alt_r = Right-Option (dictation key in button mode). List several to bind aliases across keyboards
addressing = "button"                             # "button" (two keys, default) | "keyword" (one key, no command layer)
command_hotkey = ["cmd_r"]                         # button mode: the "system" key(s) (Right-Command). Also a list of alternatives
broadcast_word = "everyone"                       # leading word that injects to all named agents
model_id = "mlx-community/parakeet-tdt-0.6b-v2"   # English-only (v3 is multilingual and drifts to Russian on short clips)
sample_rate = 16000
mic_device = ""                                   # CoreAudio input name; "" = system default. Set via `vupai mic`
fuzzy_cutoff = 82                                 # name-match strictness (0-100)
poll_interval = 0.5                               # pane-registry refresh (s)
inject_confirm_timeout = 2.0                      # wait for pasted text before Enter (s)
inject_poll_interval = 0.05
pane_command = "claude"                           # default program for voice-created panes
confirm_destructive = true                        # y/n popup before close / close-others / broadcast
confirm_timeout_s = 8.0                            # popup auto-cancels after this (s)
confirm_create_threshold = 8                      # also pop the confirm for "create N panes" when N >= this (set high to disable)
# board/read summarizer. Default: bundled streaming Haiku wrapper (speaks "read" token-by-token). Swap for codex/gemini/ollama.
# board_summarizer_cmd = "python -m vupai.claude_summarize --model claude-haiku-4-5"   # the default (computed with this interpreter)
# board_summarizer_cmd = "claude -p --model claude-haiku-4-5"   # plain claude: buffers, speaks once at the end
# Offload to a (remote) Ollama box, skipping the ~3s `claude -p` CLI cold-start per call:
# board_summarizer_cmd = "python3 /abs/path/scripts/ollama_summarize.py --host http://BOX:11434 --model qwen2.5:7b"
board_min_summary_interval = 30.0                 # per-pane floor (s) between board summaries; bounds cost
tts_stream = true                                 # speak the "read" summary sentence-by-sentence as it streams (needs a streaming summarizer)

[programs]                                        # spoken token -> argv ("" = plain shell)
claude = "claude"
shell = ""

[aliases]                                         # spoken alias -> pane name
# bot = "atlas"

[macros]                                          # spoken phrase -> list of actions
# "start the squad" = ["create 3 panes", "tile"]

[slash_commands]                                  # spoken verb -> literal injected into the pane(s)
clear = "/clear"                                  # system key: "clear [name|all]"
compact = "/compact"
```

**Addressing modes.** In `button` mode (default) you hold one of two keys: the
dictation key (`hotkey`) types your words verbatim into the focused pane, while the
system key (`command_hotkey`) interprets them as a command, a broadcast, or a
name-addressed message ("atlas, are you there?"). The key is the control signal, so
no spoken control word is needed. Each key field is a list, so you can bind several
keys to the same action (any one triggers it) and keep one config that works across
keyboards with different layouts. `keyword` mode is the legacy single-key mode: it
has no command layer - only the `broadcast_word` ("everyone ...") leads; everything
else is name-addressed or dictated verbatim to the focused pane.

## tmux tips

vupai sets the tmux options it needs at startup (`ensure_up`), so **no config is
required**. A couple of optional settings just make the multi-agent flow nicer:

```tmux
# ~/.tmux.conf (optional, pairs well with vupai)
set -g mouse on                       # click a pane to focus, scroll to read history
bind -T copy-mode-vi WheelUpPane   send -X scroll-up
bind -T copy-mode-vi WheelDownPane send -X scroll-down
```

> [!WARNING]
> Do **not** enable `extended-keys` (CSI-u) in your tmux config:
> ```tmux
> set -s extended-keys on                     # breaks vupai
> set -as terminal-features 'xterm*:extkeys'  # breaks vupai
> ```
> It re-encodes Enter, so vupai's injected text never submits in Claude Code.
> vupai forces `extended-keys off` at startup, but a later `tmux source-file`
> (config reload) flips it back on and silently breaks submission. For the same
> reason, don't override `pane-border-format` / `pane-border-status` (clobbers the
> voice-name border) or rebind `<prefix> + R` (vupai uses it to rename a pane).
> These apply **inside vupai's own session** (tmux still sources your
> `~/.tmux.conf` on vupai's dedicated server); your default tmux is untouched.

## License

[MIT](LICENSE). (Note: `pynput` is LGPL-3.0 and the Parakeet model weights are
CC-BY-4.0; both are runtime dependencies, not part of this repo's code.)
