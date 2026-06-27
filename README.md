<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/brand/vupai-lockup-dark.png">
    <img alt="vupai" src="./assets/brand/vupai-lockup.png" width="260">
  </picture>
</p>

<p align="center">
  <strong>Voice UI for AI panes</strong>: push-to-talk voice control for your tmux agent panes, on macOS, fully local.
</p>

<p align="center">
  <a href="https://pypi.org/project/vupai/"><img alt="PyPI" src="https://img.shields.io/pypi/v/vupai.svg?v=0.4.0"></a>
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-blue.svg"></a>
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/python-%3E%3D3.11-brightgreen.svg"></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-black.svg">
</p>

*vupai* (say "voo-pie") is a **V**oice **U**I for your AI **pa**nes.

Hold a key, speak, and what you say is typed into the right tmux pane: the one
you're looking at, or an agent you call by name (*"atlas, run the tests"*).
Speech-to-text runs on-device with NVIDIA Parakeet (via Apple MLX): no cloud,
no API keys.

Built for a tmux-centric workflow where you keep several coding agents and
shells open at once and want to drive them by voice without reaching for the
mouse. New panes launch an agent by default (`claude` out of the box) and should
work with other agentic coding tools (Codex, Gemini, …), though testing so far
has focused on Claude Code.

**Jump to:** [Requirements](#requirements) · [Install](#install) · [Set up](#set-up-once) · [Usage](#usage) · [Voice commands](#voice-commands) · [Supervision board](#supervision-board) · [Activity ledger](#cross-pane-activity-ledger) · [Configuration](#configuration) · [tmux tips](#tmux-tips) · [Uninstall](#uninstall)

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

Because input is voice-first, it can also ease the typing load for anyone with RSI
or hand-strain, though vupai isn't built or tested as a dedicated accessibility
tool.

## How it works

```
hold dictation key (Right-Option) → record (sox) → transcribe (Parakeet) → route → paste into a tmux pane → Enter
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

- macOS on **Apple Silicon** (M-series), macOS 13.5+ (developed on macOS 26).
- [`tmux`](https://github.com/tmux/tmux) and [`sox`](https://sox.sourceforge.net/):
  ```bash
  brew install tmux sox
  ```
- Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/), used to install the CLI:
  ```bash
  brew install uv          # or: curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

## Install

After the Homebrew step above, install the `vupai` CLI from PyPI in its own
isolated environment with [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install vupai
```

This puts `vupai` on your `PATH`. The Parakeet model (~0.6B, ~2 GB) downloads
automatically on first transcription.

To upgrade later: `uv tool upgrade vupai`.

> [!NOTE]
> Prefer the bleeding edge? Install straight from git instead:
> `uv tool install git+https://github.com/itsjrsa/vupai`.

### From source (development)

```bash
git clone git@github.com:itsjrsa/vupai.git
cd vupai
uv sync            # creates .venv and installs everything (incl. the MLX runtime)
```

Run the CLI with `uv run vupai …` from the repo, or see the live-reload loop
(`vupai reload` / `vupai --reload`) in [AGENTS.md](AGENTS.md).

To install **this** checkout as a real `vupai` on your `PATH` (no PyPI), instead
of `uv run`:

```bash
uv tool install .            # from the repo root; re-run after pulling changes
```

> [!NOTE]
> The examples below use the bare `vupai` command (the installed tool). If you're
> running from a source checkout, prefix each one with `uv run` (e.g. `uv run
> vupai setup`).

Contributing? See [AGENTS.md](AGENTS.md).

## Set up (once)

After install, run the interactive bootstrap:

```bash
vupai setup
```

It handles everything first-run: checks the Homebrew tools, captures consent for
the local transcript journal, lets you pick a mic and your push-to-talk key(s),
downloads the speech model up front (so the first hotkey press doesn't stall on a
silent fetch), then deep-links you to each macOS permission pane that still needs
your terminal app enabled. Safe to re-run any time.

### macOS permissions

vupai needs three permissions, granted to **your terminal app** (Ghostty / iTerm /
Terminal / …) under **System Settings → Privacy & Security**: **Accessibility**,
**Input Monitoring**, and **Microphone**. `vupai setup` deep-links you to each; to
audit them on their own, run `vupai doctor` (it probes each and prints the exact
System-Settings path for anything missing).

> [!WARNING]
> macOS grants these to the terminal binary, not the script, so the hotkey and
> mic silently fail until you grant them. If voice seems dead, check this first
> (`vupai doctor`).

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

> [!IMPORTANT]
> **It's still tmux.** The voice layer sits on top; every normal tmux binding keeps
> working. Detach with `<prefix> d`, split panes by hand (`<prefix> %` / `"`),
> switch with `<prefix>`-arrow, scroll/copy-mode, resize, your own custom
> keybindings: all unchanged. Use voice when it's faster, the prefix key when it's
> not. (Your `~/.tmux.conf` is sourced too, with the few exceptions noted in
> [tmux tips](#tmux-tips).)

Once attached, you talk to vupai with **two push-to-talk keys**:

| Key | Config | Default | Hold and speak to… |
|---|---|---|---|
| **Dictation key** | `hotkey` | Right-Option | Type your words into the **focused** pane. |
| **System key** | `command_hotkey` | Right-Command | Run a **voice command** (below). The key is the signal, so there is no spoken control word; vupai acts on the panes instead of typing. |

Both defaults are customizable: set `hotkey` / `command_hotkey` in the config (each
takes a list, so you can bind several keys to one action).

> vupai only listens for keyboard keys. To use a mouse button or other input as a
> push-to-talk key, remap it to a keyboard key (e.g. `F13`) with a tool like
> [Karabiner-Elements](https://karabiner-elements.pqrs.org/) or BetterTouchTool,
> then bind that key here.

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
| *"louder"* / *"quieter"* | Nudge readback volume (`tts_volume`, macOS `say` only) |
| *"atlas, run the tests"* | Not a command, so it falls through to **name addressing** |

## Commands

Run `vupai --help` for the full command list (and `vupai <command> --help` for a
specific one). The everyday ones are in [Usage](#usage) above.

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
  swappable command (`board_summarizer_cmd`), not a fixed model. vupai appends the
  pane's scrollback tail as the command's last argument and takes its last stdout
  line as the summary, so any command that follows that contract works:

  | To summarize with… | Set `board_summarizer_cmd` to |
  |---|---|
  | Claude Haiku (default, streaming) | `python -m vupai.claude_summarize --model claude-haiku-4-5` |
  | Claude (plain, buffered) | `claude -p --model claude-haiku-4-5` |
  | Codex | `codex exec` |
  | Gemini | `gemini -p` |
  | Ollama (local/remote) | `python scripts/ollama_summarize.py --host http://BOX:11434 --model qwen2.5:7b` |

  The model is whatever that command uses (e.g. Codex's own config/profile). If the
  command is missing or fails, the board falls back to a non-LLM last-line summary.
- **Cheap by design.** A pane is summarized only when it *settles* (finishes a
  burst of work), skipped when nothing changed, and throttled per pane
  (`board_min_summary_interval`).
- **Speak it too.** *"read board"* reads the digest aloud; *"read atlas"* reads a
  single pane.

One board per session. Close the pane to stop it.

## Cross-pane activity ledger

When several agents share one working tree, they edit files unaware of each other
and can clobber each other's uncommitted work. The **activity ledger** is a
best-effort, **pull-only** awareness surface: a background poller records which
pane last touched which file in each git tree, so you (or your agents) can spot
overlaps before they become conflicts.

- **What it records.** Per git tree, in a `.vupai/` directory at the tree root:
  `activity.current.json` (latest state per pane) plus `activity.jsonl` (history).
  Each entry names the pane, the files it touched, a coverage flag
  (`exact`, `git-delta`, or `churn-only` for an active pane no file could be
  pinned on), and any `contended_with` panes editing the same file.
  `.vupai/` is auto-gitignored, so it never appears in `git status`.
- **How it decides.** `git status` provides *what* changed; each pane's scrollback
  provides *which pane*; their intersection is the attribution. It is post-write on
  a ~2s poll, so it *reduces* clobbering by surfacing overlaps; it does not prevent
  a sub-2-second race, and it never blocks or injects into a pane.
- **Read it.**
  - `vupai activity` shows the current ledger, grouped by tree.
  - `vupai activity --stats` reports contention and attribution rates (use these to
    judge whether it earns its keep in your workflow).
  - Say **"activity"** (or *"who's editing"*) to hear the digest.

### Let your agents use it

The ledger is pull-only by design, so nothing forces an agent to read it. If you
want your agents to coordinate through it, tell them to: add a few lines to your
project's `AGENTS.md` (or `CLAUDE.md`, or whatever instructions file your agent
loads):

```markdown
## Before editing a shared file
This repo may have several agents working in sibling panes at once. Before you
edit a file, read `.vupai/activity.current.json` at the repo root. If another
pane is listed as touching that file, or the file appears under a pane's
`contended_with`, stop and coordinate or pick different work instead of
overwriting it (the sibling's edits are uncommitted and you would clobber them).
A pane with coverage `churn-only` is active but its file is unknown: treat the
tree as contended and be cautious.
```

This is opt-in and best-effort: an agent consults the ledger only if its
instructions tell it to, and even then it will not monitor the file continuously
on its own. For a hard guarantee, give each agent its own git worktree (a planned
opt-in) so panes physically cannot clobber one another.

### Reviewing uncommitted changes (`vupai review`)

`vupai review` opens a live, in-terminal review of every uncommitted change
across your panes, grouped by the pane that touched it. It is a read-only,
pull-only view: it runs `git diff` (the authoritative change set) and joins the
activity ledger for attribution. It never stages, commits, or writes to a pane.

- Left: files grouped under the pane editing them, with `+`/`-` counts. Files
  touched by two or more panes are flagged `!` and float to the top.
- Right: the selected file's diff, updating as you move. A file only one pane
  touched shows that pane's exact diff (its whole change is that agent's work).
  A file two or more panes edited shows the combined diff, flagged because it
  cannot be split per agent in a shared tree without worktree isolation.
- A trailing **unattributed** bucket lists changed files no pane claimed
  (including untracked files); names are never fabricated.
- It re-polls about every two seconds, so it tracks edits as they land.

Keys: up/down move, Enter (or `o`) opens the file in `$EDITOR`, space folds a
pane group, `p` pauses live polling, `r` refreshes now, `q` quits.

## Configuration

vupai reads `~/.config/vupai/config.toml`. `vupai setup` writes it on first run,
pre-filled with **every key at its default and an inline comment explaining it**, so
the file itself is the reference. It's left untouched if one already exists, and
`vupai config --init` tops it up with any keys a newer version added without
disturbing your edits. Editing is optional; open the file to see them all.

**Applying changes.** The daemon reads its config once at startup, so a change
takes effect only after it respawns. The interactive commands (`vupai setup`,
`vupai keys`, `vupai mic`) apply their change automatically: if a daemon is
running, they reload it for you. But edits you make **by hand** to
`config.toml` or `hosts.toml` are not watched, so run `vupai reload` to pick
them up (or `vupai --reload` to reload and attach in one step).

The keys most people touch:

| Key | What it does |
|---|---|
| `hotkey` / `command_hotkey` | The dictation and system push-to-talk keys (pynput names; each a list, so you can bind several). |
| `pane_command` | Default program for voice-created panes (e.g. `claude`). |
| `broadcast_word` | Leading word that injects to every named agent (default `everyone`). |
| `board_summarizer_cmd` | Command that summarizes panes for the board and `read` (see [Supervision board](#supervision-board)). |
| `[programs]` / `[aliases]` / `[macros]` / `[slash_commands]` | Spoken-token tables: program names, pane-name aliases, phrase macros, and slash verbs. |

### Remote machines (SSH)

The *"ssh box"* / *"connect to box"* voice command opens a new pane and SSHes into
a host you name. Hosts live in a separate file, `~/.config/vupai/hosts.toml`. Write
a commented template with:

```bash
vupai hosts --init        # scaffold ~/.config/vupai/hosts.toml
vupai hosts               # list what's configured
```

Each host is one table; only `host` is required (SSH key auth must already work):

```toml
[hosts.box]
user = "me"               # optional; omit to use ~/.ssh/config defaults
host = "box.example.com"  # required: hostname/IP or an ssh-config Host alias
port = 22                 # optional
program = "claude"        # optional; omit to land in a plain login shell (default)
```

Say the table name (*"ssh box"*) to connect. By default you land in a login shell,
so you can `cd` into a project first; set `program` to auto-start an agent instead.

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

## Roadmap

vupai is young and evolving. A few things on the horizon (in no particular
order):

- **Tighter pane-state and activity awareness** so routing and the board react
  faster to what each agent is actually doing. *(in progress)*
- **Broader agent-CLI coverage**: validate the flow end-to-end with Codex,
  opencode, Gemini, and other agentic tools (testing so far has centered on
  Claude Code).
- **Smarter addressing**: more forgiving name matching and disambiguation when
  several agents answer to similar names.
- **More voice commands** for everyday tmux moves, so less reaching for the
  prefix key.
- **Linux support** is a long shot (the speech stack is Apple-MLX-only today),
  but a pluggable transcription backend would open the door.

Ideas and contributions are welcome: open an issue or PR.

## Uninstall

```bash
vupai down                       # stop the background daemon
vupai cleanup                    # revert any leftover settings on your default tmux server
uv tool uninstall vupai          # remove the CLI
```

That removes the program. To also delete what it created on disk:

```bash
rm -rf ~/.config/vupai           # config, hosts, daemon log, journal
rm -rf ~/.cache/huggingface/hub/models--mlx-community--parakeet-tdt-0.6b-v2   # the ~2 GB speech model
```

The Homebrew tools (`tmux`, `sox`) are general-purpose; remove them only if nothing
else needs them (`brew uninstall tmux sox`). The macOS permissions were granted to
your terminal app, not to vupai, so leave them unless you want to revoke them by
hand under **System Settings → Privacy & Security**.

## License

[MIT](LICENSE). (Note: `pynput` is LGPL-3.0 and the Parakeet model weights are
CC-BY-4.0; both are runtime dependencies, not part of this repo's code.)
