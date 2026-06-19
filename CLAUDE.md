# voxpane — voice control for tmux agent panes

Push-to-talk voice control over a tmux-based multi-agent workflow on macOS.
Hold a hotkey, speak, and the transcript is injected into the right tmux pane —
the focused one by default, or an agent addressed by name ("Nova, run the tests").

## Status

v1 implemented and on `master` (127 unit tests pass, `ruff` clean). Validated by
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
- `voxpane` — ensure tmux + the voice daemon window, then attach (default, no subcommand)
- `voxpane up` / `voxpane down` — start / stop the daemon (`down` also kills the voice window)
- `voxpane name <name> [pane]` — label a pane (rejects confusable names; defaults to focused)
- `voxpane status` — list panes, daemon pid, permission state
- `voxpane _daemon` — hidden; the long-running process the voice window executes

## Architecture

Single local daemon, small modules behind narrow interfaces. Pipeline:

```
hotkey → recorder → asr → router → injector → feedback   (+ tmux pane registry)
```

| File | Responsibility |
|---|---|
| `src/voxpane/cli.py`, `__main__.py` | `voxpane` subcommands; `ensure_up`; spawns the daemon |
| `src/voxpane/daemon.py` | orchestrates press→record→transcribe→route→inject→feedback |
| `src/voxpane/hotkey.py` | global push-to-talk via `pynput`, debounced (Right-Option) |
| `src/voxpane/recorder.py` | `sox rec` → wav, SIGINT to stop; exports `MIN_WAV_BYTES` |
| `src/voxpane/asr.py` | `parakeet-mlx` `Transcriber` Protocol, lazy `warm()` + cache |
| `src/voxpane/router.py` | name cascade exact→rapidfuzz→metaphone, number-in-window, focus fallback, near-tie ambiguity |
| `src/voxpane/registry.py` | `Pane` + `PaneRegistry` parsed from `tmux list-panes` |
| `src/voxpane/injector.py` | paste → poll `capture-pane` → Enter (the safety core) |
| `src/voxpane/tmuxio.py` | thin exact-argv wrappers over the `tmux` CLI |
| `src/voxpane/feedback.py` | status to stdout / `display-message` on the target pane |
| `src/voxpane/permissions.py` | best-effort macOS permission probes + `hints` |
| `src/voxpane/config.py` | TOML config at `~/.config/voxpane/config.toml` + defaults |

The daemon reaches tmux only through `tmuxio` (the `tmux` CLI); the hotkey is
global, so the daemon never owns the terminal.

## Invariants & gotchas (don't break these)

- **Injection safety:** `injector.inject` pastes, then **polls `capture-pane`
  until the pasted text appears before sending Enter** — never a fixed sleep,
  retries once, and **never sends Enter on an unconfirmed paste**. This is the
  single most important property; it has dedicated tests.
- **`tmuxio.run(args)` prepends `tmux`** — callers pass argv WITHOUT a leading `"tmux"`.
- **Target the immutable `pane_id` (`%N`)**, never a positional index.
- **Keep tmux `extended-keys` off** (set in `ensure_up`) so Enter submits in Claude Code.
- **Unnamed panes:** tmux titles an untitled pane with its own id (`%1`). Router
  name-matching and the ASR hints **skip panes where `name == id`**; number
  routing still considers them.
- **ASR is kept warm** (model loaded once via `warm()`); the first call is otherwise multi-second.
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
