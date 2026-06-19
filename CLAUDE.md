# voxpane — voice control for tmux agent panes

Push-to-talk voice control over a tmux-based multi-agent workflow on macOS.
Hold a hotkey, speak, and the transcript is injected into the right tmux pane —
the focused one by default, or an agent addressed by name ("Nova, run the tests").

## Status

v1 implemented and on `master` (148 unit tests pass, `ruff` clean). Validated by
**unit tests only** — the `@integration` (real tmux) and `@slow` (real Parakeet
model + mic) suites and the live daemon must run on a macOS Apple-Silicon machine.
The design spec + implementation plan live under `docs/superpowers/`
(**local-only, gitignored** — see Conventions).

## Commands

```bash
uv sync                                              # install deps (or: uv pip install -e .)
uv run pytest -m "not integration and not slow" -q   # unit suite (no tmux/mic/model)
uv run pytest -m integration -q                      # needs a real tmux (isolated -L socket)
uv run pytest -m slow -q                             # needs the real Parakeet model + a wav fixture
uv run ruff check .                                  # lint
voxpane doctor                                         # check macOS permissions, print fix steps
```

`voxpane` CLI (entry point `voxpane.cli:main`):
- `voxpane` — ensure tmux + spawn the voice daemon (detached), then attach (default, no subcommand)
- `voxpane up` / `voxpane down` — start / stop the daemon (`down` SIGTERMs the pid; also clears a legacy voice window)
- `voxpane name <name> [pane]` — label a pane (rejects confusable names; defaults to focused)
- `voxpane autoname [pane]` — assign the next free callsign from the pool to a pane unless already named; driven by the tmux pane-creation hooks (also usable by hand). `<prefix>+R` renames the active pane via this path's sibling `voxpane name`
- `voxpane status` — list panes, daemon pid + log path, permission state
- `voxpane _daemon` — hidden; the long-running daemon process (spawned detached, logs to `~/.config/voxpane/daemon.log`)

## Architecture

Single local daemon, small modules behind narrow interfaces. Pipeline:

```
hotkey → recorder → asr → router → injector → feedback   (+ tmux pane registry)
```

| File | Responsibility |
|---|---|
| `src/voxpane/cli.py`, `__main__.py` | `voxpane` subcommands; `ensure_up`; spawns the daemon **detached** (`_spawn_daemon`, `start_new_session=True`) |
| `src/voxpane/daemon.py` | orchestrates press→record→transcribe→route→inject→feedback; listener callbacks enqueue, main-thread consumer processes |
| `src/voxpane/hotkey.py` | global push-to-talk via `pynput`, debounced (Right-Option) |
| `src/voxpane/recorder.py` | `sox rec` → wav, SIGINT to stop; exports `MIN_WAV_BYTES` |
| `src/voxpane/asr.py` | `parakeet-mlx` `Transcriber` Protocol, lazy `warm()` + cache |
| `src/voxpane/router.py` | name cascade exact→rapidfuzz→metaphone, number-in-window, focus fallback, near-tie ambiguity; `CALLSIGNS` pool + `next_callsign` (auto-name picker) |
| `src/voxpane/registry.py` | `Pane` + `PaneRegistry` parsed from `tmux list-panes` |
| `src/voxpane/injector.py` | paste → poll `capture-pane` → Enter (the safety core) |
| `src/voxpane/tmuxio.py` | thin exact-argv wrappers over the `tmux` CLI |
| `src/voxpane/feedback.py` | status to stdout / `display-message` on the target pane |
| `src/voxpane/permissions.py` | best-effort macOS permission probes + `hints` |
| `src/voxpane/config.py` | TOML config at `~/.config/voxpane/config.toml` + defaults |

The daemon reaches tmux only through `tmuxio` (the `tmux` CLI); the hotkey is
global, so the daemon never owns the terminal. It runs as a **detached
background process under the terminal app, NOT inside a tmux window** (see
Invariants) and talks to tmux purely via the CLI.

## Invariants & gotchas (don't break these)

- **Injection safety:** `injector.inject` pastes, then **polls `capture-pane`
  until the pasted text appears before sending Enter** — never a fixed sleep,
  retries once, and **never sends Enter on an unconfirmed paste**. This is the
  single most important property; it has dedicated tests.
- **`tmuxio.run(args)` prepends `tmux`** — callers pass argv WITHOUT a leading `"tmux"`.
- **Target the immutable `pane_id` (`%N`)**, never a positional index.
- **Keep tmux `extended-keys` off** (set in `ensure_up`) so Enter submits in Claude Code.
- **Voice names live in the `@voxpane_name` per-pane user option, NOT `pane_title`.**
  The target apps own the pane title: Claude Code overwrites it with `✳ Claude Code`
  on startup, so a name stored via `select-pane -T` is clobbered (and every Claude
  pane ends up with the *same* title → routing breaks). `voxpane name` writes
  `set -p @voxpane_name`; `PANE_FORMAT` reads `#{@voxpane_name}`; the pane border
  shows the voice name when set, else the app title. **Never store the name in
  `pane_title`.**
- **Unnamed panes:** when `@voxpane_name` is unset the field is empty; `parse_panes`
  falls back to the pane id so `name == id`. Router name-matching and the ASR hints
  **skip panes where `name == id`**; number routing still considers them.
- **Auto-naming:** `ensure_up` sets `after-split-window` + `after-new-window` hooks
  (and binds `<prefix>+R`) so every newly created pane runs `voxpane autoname` and
  gets the next free `CALLSIGNS` entry. The hook targets `#{pane_id}` (the pane
  active *after* the split), so it relies on the new pane being focused (the tmux
  default for an interactive split); a detached `split-window -d` would name the
  wrong pane. `autoname` is **idempotent** (skips a pane whose `name != id`).
  The hooks fire only for panes created *after* they're installed, so `new-session`'s
  **initial pane fires no hook** — `ensure_up` therefore also runs
  `_autoname_unnamed_panes()`, a one-time idempotent sweep that names the initial
  pane (and any pre-existing unnamed panes when attaching to a running server).
  Hook/binding callbacks run via tmux `run-shell` (`/bin/sh`, no venv on PATH), so
  `_self_cmd()` invokes them with the absolute `sys.executable -m voxpane`.
- **ASR is kept warm** (model loaded once via `warm()`); the first call is otherwise multi-second.
- **Daemon must run OUTSIDE tmux** (`_spawn_daemon` detaches it under the terminal
  app). A process *inside* a tmux window has the long-lived tmux server as its
  macOS "responsible process", which lacks Input Monitoring — so the global
  `pynput` listener is silently never fed key events (the hotkey looks dead).
  Running detached under the terminal app inherits the grants the user already
  gave it. **Never move the daemon back into a tmux window.**
- **MLX is thread-local:** `parakeet-mlx`/MLX bind the GPU stream to the thread
  that first uses it, so **`warm()` and every `transcribe()` MUST run on the same
  OS thread** or you get `RuntimeError: no Stream(gpu, 0) in current thread`.
  Therefore the `pynput` listener callbacks (`on_press`/`on_release`) stay thin —
  they only start/stop the recorder and **enqueue the wav** — and `daemon.run()`
  consumes that queue on the **main thread**, where it also called `warm()`,
  doing transcribe→route→inject there. Keeping heavy work off the listener thread
  also prevents macOS from disabling the (slow) event tap. Don't call MLX, tmux,
  or `inject` from the listener thread.
- **macOS permissions** (Accessibility + Input-Monitoring + Microphone) are granted
  to the *terminal app*, not the script — they silent-fail otherwise. Use `voxpane doctor`.
- Tests inject collaborators (`io=`, `lister=`, `route_fn=`, `recorder_factory=`…)
  so units run with fakes — no real tmux/mic/model in the unit suite.

## Design decisions (settled rationale)

Hybrid routing (focus default + leading-name override) · push-to-talk, hold
Right-Option, no wake word · voice input only (TTS deferred) · Python daemon (not
Swift/native, not a browser app) · Parakeet via `parakeet-mlx` · **v1 drives Claude
Code panes only** (Codex/OpenCode have known send-keys submit bugs).

## Conventions

- Spec/plan docs under `docs/superpowers/` (and `.superpowers/` SDD scratch) are
  **local-only — never commit them** (gitignored). When a skill says "commit the
  design/plan doc," skip that step in this repo.
- Code comments in English. TDD with pytest; frequent small commits.
- Conventional commit messages, **no Claude attribution / co-authored-by lines**.
  Never push to `master` without asking.

## Known limitations / deferred

- **ASR name biasing is a no-op** — the installed `parakeet-mlx` `transcribe()` has
  no `hotwords` kwarg, so `asr.py` forwards-then-falls-back. Router name-matching is
  unaffected. For real biasing, swap the `Transcriber` to whisper.cpp/faster-whisper
  with `initial_prompt` (the Protocol keeps this contained).
- `router.route`'s `ambiguity_margin` is hardcoded (5); not exposed in `Config`.
- `tests/fixtures/tiny.wav` is absent → the `@slow` smoke test self-skips.
