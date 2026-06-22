# vupai - voice control for tmux agent panes

Push-to-talk voice control over a tmux-based multi-agent workflow on macOS.
Hold a hotkey, speak, and the transcript is injected into the right tmux pane:
the focused one by default, or an agent addressed by name ("Nova, run the tests").

> This file is the single source of truth for all AI coding agents (Claude Code,
> Codex, opencode, Cursor, Aider, …). `CLAUDE.md` is a stub that points here.

## Status

v1 implemented and on `master` (448 unit tests pass, `ruff` clean). Validated by
**unit tests only** - the `@integration` (real tmux) and `@slow` (real Parakeet
model + mic) suites and the live daemon must run on a macOS Apple-Silicon machine.
The design spec + implementation plan live under `docs/superpowers/`
(**local-only, gitignored** - see Conventions).

## Commands

```bash
uv sync                                              # install deps (or: uv pip install -e .)
uv run pytest -m "not integration and not slow" -q   # unit suite (no tmux/mic/model)
uv run pytest -m integration -q                      # needs a real tmux (isolated -L socket)
uv run pytest -m slow -q                             # needs the real Parakeet model + a wav fixture
uv run ruff check .                                  # lint
vupai doctor                                         # check macOS permissions, print fix steps
vupai setup                                          # interactive: probe + deep-link each missing-permission pane
```

`vupai` CLI (entry point `vupai.cli:main`):
- `vupai [--reload]` - ensure tmux + spawn the voice daemon (detached), then attach (default, no subcommand). `--reload` respawns the daemon first (= `vupai reload && vupai` in one invocation) so source edits load before attaching - the dogfooding loop. **Run from inside a tmux pane it skips the attach** (`tmuxio.inside_tmux()` / `$TMUX`): `tmux attach` refuses to nest, so `_cmd_default` respawns the daemon, prints a note, and returns - so `--reload` from within the session degrades to a plain `reload` instead of failing. Use bare `vupai reload` (no attach) as the in-pane dogfooding loop.
- `vupai up` / `vupai down` - start / stop the daemon (`down` SIGTERMs the recorded pid; the daemon is a detached process, not a tmux window)
- `vupai reload` - `down` + `ensure_up` in one step; respawns the daemon so source edits take effect (the daemon loads modules once at spawn, so a live one runs stale code). For dogfooding vupai on itself (or `vupai --reload` to also re-attach)
- `vupai name <name> [pane]` - label a pane (rejects confusable names; defaults to focused)
- `vupai autoname [pane]` - assign the next free callsign from the pool to a pane unless already named; driven by the tmux pane-creation hooks (also usable by hand). `<prefix>+R` renames the active pane via this path's sibling `vupai name`
- `vupai status` - list panes, daemon pid + log path, permission state
- `vupai mic [index|name|default] [--force]` - no arg lists CoreAudio **input** devices (marks the system default and the current pin); an index/exact-name pins that device into `config.toml` via `config.set_mic_device` (a merge writer that preserves comments/other keys), the literal `default` clears the pin. Before pinning a named device it runs `audio.probe_capture` (a 0.4s `rec` with `AUDIODEV` set) and **refuses an unusable device** (exit 1) rather than letting it silently yield "no audio captured" at speech time; `--force` pins anyway, and `default` (clearing the pin) skips the probe. The probe catches the failure mode where a USB mic exposes a speaker and a mic under the **same CoreAudio name** and sox's `AUDIODEV` name-match grabs the output (`can not get audio device properties`), plus disconnected/muted inputs and missing Microphone permission. Selection persists; a running daemon needs `vupai reload` to apply it. Enumeration is `audio.list_input_devices` (`system_profiler -json SPAudioDataType`, ~1s).
- `vupai keys` - no-arg, interactive: prints the current addressing mode + push-to-talk key(s), then runs the re-runnable picker (`_prompt_hotkey_setup`). Choose the addressing mode (`button`/`keyword`), then pick the dictation key (and, in button mode, the command key) from a curated menu (`hotkey.PTT_KEYS`), by exact pynput key name, or by **pressing the key** (`p` → `hotkey.capture_key`, a one-shot pynput listen). Bare Enter keeps the current value; button mode **rejects two equal keys**. Persists `addressing`/`hotkey`/`command_hotkey` via `config.set_hotkey_config` (a merge writer preserving comments/other keys); a running daemon needs `vupai reload` to apply. NOTE: macOS pynput collapses the left modifiers (`alt_l`→`alt`, `cmd_l`→`cmd`, `ctrl_l`→`ctrl`), so capture reports those bare names and `PTT_KEYS` uses them for the left-side entries.
- `vupai config --init` - (re)write `~/.config/vupai/config.toml` as a full annotated template: every `Config` field present, commented out at its default, with doc prose, so the whole tunable surface is discoverable without reading source. Backs up any existing file to `config.toml.bak` first (`config.regenerate_config`). First-run `vupai setup` writes this same annotated file (with the chosen journal/mic/hotkey keys uncommented).
- `vupai setup` - interactive permission bootstrap: detects the terminal app from `TERM_PROGRAM`, probes each permission (which triggers the macOS prompts), then `open`s the exact Settings deep-link pane for any that are missing and prints the `tccutil reset` recovery command. **Cannot grant on the user's behalf** - macOS TCC requires a human click; setup removes the navigation, not the consent. Deep-link/app-detect/open helpers live in `permissions.py` (`terminal_app`, `fixes`, `open_settings_pane`), injectable for tests. **First run only** (no `config.toml` yet), it also prompts for journaling consent (`journal_enabled` + `journal_keep_audio`) and writes the full annotated config via `config.write_full_config` (the chosen journal keys uncommented, every other key commented at its default); once a config file exists the prompt is skipped so re-running to confirm permissions never re-asks. It **always** runs a re-runnable mic-selection step (`_prompt_mic_setup`; bare Enter keeps the current device) **and a re-runnable hotkey step** (`_prompt_hotkey_setup`; bare Enter / non-interactive stdin keeps the current keys).
- `vupai _daemon` - hidden; the long-running daemon process (spawned detached, logs to `~/.config/vupai/daemon.log`)

## Architecture

Single local daemon, small modules behind narrow interfaces. Pipeline:

```
hotkey → recorder → asr → router → injector → feedback   (+ tmux pane registry)
```

| File | Responsibility |
|---|---|
| `src/vupai/cli.py`, `__main__.py` | `vupai` subcommands; `ensure_up`; spawns the daemon **detached** (`_spawn_daemon`, `start_new_session=True`) |
| `src/vupai/daemon.py` | orchestrates press→record→transcribe→route→inject→feedback; listener callbacks enqueue, main-thread consumer processes |
| `src/vupai/hotkey.py` | global push-to-talk via `pynput`, debounced (Right-Option); also `PTT_KEYS` (curated menu), `valid_key`, and `capture_key` (one-shot press-a-key capture) for the `vupai keys` setup |
| `src/vupai/hotkey.py` (`MultiHotkey`) | button mode: one pynput listener over two PTT keys, each independently debounced |
| `src/vupai/recorder.py` | `sox rec` → wav, SIGINT to stop; exports `MIN_WAV_BYTES`; optional `device` → `AUDIODEV` env |
| `src/vupai/audio.py` | enumerate CoreAudio **input** devices (`system_profiler -json`); `resolve_device` (configured name → present? else fall back to default + warning) |
| `src/vupai/asr.py` | `parakeet-mlx` `Transcriber` Protocol, lazy `warm()` + cache |
| `src/vupai/router.py` | name cascade exact→rapidfuzz→metaphone, number-in-window, focus fallback, near-tie ambiguity; `CALLSIGNS` pool + `next_callsign` (auto-name picker) |
| `src/vupai/registry.py` | `Pane` + `PaneRegistry` parsed from `tmux list-panes` |
| `src/vupai/injector.py` | paste → poll `capture-pane` → Enter (the safety core) |
| `src/vupai/tmuxio.py` | thin exact-argv wrappers over the `tmux` CLI |
| `src/vupai/feedback.py` | status to stdout / `display-message` on the target pane |
| `src/vupai/permissions.py` | best-effort macOS permission probes + `hints` |
| `src/vupai/config.py` | TOML config at `~/.config/vupai/config.toml` + defaults; `ANNOTATED_TEMPLATE` + `render_config` are the single source of truth for the generated file; `write_full_config` writes a fresh full annotated file (first-run `setup`; does NOT merge) and `regenerate_config` rewrites it (backing up to `.bak`, for `vupai config --init`); `set_mic_device` / `set_hotkey_config` MERGE keys in place via shared `_merge_scalar_keys` (preserve comments/other keys, uncomment a commented default in place) |
| `src/vupai/commands.py` | parse control-word utterances into `Command`s and execute them (create/macro/focus/swap/close/zoom/slash/broadcast); interpretation split from execution |
| `src/vupai/journal.py` | append-only JSONL utterance trail (transcript + decision + outcome) at `~/.config/vupai/journal.jsonl`; opt-in ring-bounded audio retention for offline misfire replay |

The daemon reaches tmux only through `tmuxio` (the `tmux` CLI); the hotkey is
global, so the daemon never owns the terminal. It runs as a **detached
background process under the terminal app, NOT inside a tmux window** (see
Invariants) and talks to tmux purely via the CLI.

## Invariants & gotchas (don't break these)

- **Injection safety:** `injector.inject` pastes, then **polls `capture-pane`
  until the pasted text appears before sending Enter** - never a fixed sleep,
  retries once, and **never sends Enter on an unconfirmed paste**. This is the
  single most important property; it has dedicated tests.
- **`tmuxio.run(args)` prepends `tmux`** - callers pass argv WITHOUT a leading `"tmux"`.
- **Target the immutable `pane_id` (`%N`)**, never a positional index.
- **Keep tmux `extended-keys` off** (set in `ensure_up`) so Enter submits in Claude Code.
- **Voice names live in the `@vupai_name` per-pane user option, NOT `pane_title`.**
  The target apps own the pane title: Claude Code overwrites it with `✳ Claude Code`
  on startup, so a name stored via `select-pane -T` is clobbered (and every Claude
  pane ends up with the *same* title → routing breaks). `vupai name` writes
  `set -p @vupai_name`; `PANE_FORMAT` reads `#{@vupai_name}`; the pane border
  shows the voice name when set, else the app title. **Never store the name in
  `pane_title`.**
- **The launched program lives in `@vupai_program`** (e.g. `claude`), set
  alongside `@vupai_name` at pane creation (`_exec_create`, and the initial pane
  in `ensure_up`) for the same reason: agents overwrite `pane_title` with their
  own conversation summary, which would otherwise erase the program identity.
  The border renders `name · program · pane_title`, each segment conditional so
  missing ones collapse. `program_label()` reduces the program string to its
  basename (`/usr/bin/codex --foo` → `codex`; `""` → omitted).
- **Unnamed panes:** when `@vupai_name` is unset the field is empty; `parse_panes`
  falls back to the pane id so `name == id`. Router name-matching and the ASR hints
  **skip panes where `name == id`**; number routing still considers them.
- **Auto-naming:** `ensure_up` sets `after-split-window` + `after-new-window` hooks
  (and binds `<prefix>+R`) so every newly created pane runs `vupai autoname` and
  gets the next free `CALLSIGNS` entry. The hook targets `#{pane_id}` (the pane
  active *after* the split), so it relies on the new pane being focused (the tmux
  default for an interactive split); a detached `split-window -d` would name the
  wrong pane. `autoname` is **idempotent** (skips a pane whose `name != id`).
  The hooks fire only for panes created *after* they're installed, so `new-session`'s
  **initial pane fires no hook** - `ensure_up` therefore also runs
  `_autoname_unnamed_panes()`, a one-time idempotent sweep that names the initial
  pane (and any pre-existing unnamed panes when attaching to a running server).
  Hook/binding callbacks run via tmux `run-shell` (`/bin/sh`, no venv on PATH), so
  `_self_cmd()` invokes them with the absolute `sys.executable -m vupai`.
- **ASR is kept warm** (model loaded once via `warm()`); the first call is otherwise multi-second.
- **Mic selection resolves once at daemon startup, never per key-press.**
  `sox` records from the system default input unless `AUDIODEV` is set in its
  env (the `device` arg threads to that). Enumeration shells out to
  `system_profiler` (~1s) - far too slow for the press→record path - so
  `_cmd_daemon` calls `audio.resolve_device(cfg.mic_device)` at spawn: a pinned
  device that's absent falls back to the system default with a logged warning.
  Reconnecting a device (e.g. AirPods) after the daemon is up needs `vupai
  reload`, same as any other config change (config loads once at spawn).
- **Daemon must run OUTSIDE tmux** (`_spawn_daemon` detaches it under the terminal
  app). A process *inside* a tmux window has the long-lived tmux server as its
  macOS "responsible process", which lacks Input Monitoring - so the global
  `pynput` listener is silently never fed key events (the hotkey looks dead).
  Running detached under the terminal app inherits the grants the user already
  gave it. **Never move the daemon back into a tmux window.**
- **MLX is thread-local:** `parakeet-mlx`/MLX bind the GPU stream to the thread
  that first uses it, so **`warm()` and every `transcribe()` MUST run on the same
  OS thread** or you get `RuntimeError: no Stream(gpu, 0) in current thread`.
  Therefore the `pynput` listener callbacks (`on_press`/`on_release`) stay thin -
  they only start/stop the recorder and **enqueue the wav** - and `daemon.run()`
  consumes that queue on the **main thread**, where it also called `warm()`,
  doing transcribe→route→inject there. Keeping heavy work off the listener thread
  also prevents macOS from disabling the (slow) event tap. Don't call MLX, tmux,
  or `inject` from the listener thread.
- **macOS permissions** (Accessibility + Input-Monitoring + Microphone) are granted
  to the *terminal app*, not the script - they silent-fail otherwise. Use `vupai doctor`.
- Tests inject collaborators (`io=`, `lister=`, `route_fn=`, `recorder_factory=`…)
  so units run with fakes - no real tmux/mic/model in the unit suite.
- **Addressing mode (`addressing` config):** `button` (default) uses two keys:
  the `hotkey` (dictation) injects verbatim into the focused pane (no parse, no
  name routing), and the `command_hotkey` (system, default Right-Command) runs the
  command layer with `addressing="button"` (no control word; a non-command falls
  through to route+inject and is never swallowed as `unknown`). `keyword` is the
  legacy single-PTT-key mode and **has no command layer** - only the spoken
  `broadcast_word` leads; everything else falls through to the router (name
  addressing) or verbatim focus dictation. The daemon threads a per-utterance
  `mode` through its jobs queue as `(wav, mode)`. Bad button config (duplicate or
  unknown key names) falls back to a single keyword `Hotkey` with a feedback error.
- **No control word.** Commands are signalled by the *key* (button mode's system
  key), not a spoken magic word - `control_word` was removed. Only `broadcast_word`
  (default "everyone") still leads as a spoken word, in both modes.
- **Vocative filler peel (exact-anchor).** Leading fillers ("okay Atlas ...", "um
  create two panes") are peeled before addressing, in two places that share
  `router._FILLERS` + `router._peel_fillers` (cap 2 tokens): (1) `route()` retries
  an **exact name match only** on the peeled token - fuzzy/phonetic/number are NOT
  retried, so "okay member ..." can't hit `ember` and "okay two ..." can't route to
  pane 2; (2) `parse_command` button mode peels before the **verb** parse, but NOT
  before `broadcast_word` (mass-broadcast blast radius). The peel is kept only on a
  hit; on any miss the **original transcript is injected verbatim** (non-destructive,
  so plain dictation that starts with a filler is never corrupted). The button
  dictation key never reaches either path (verbatim). Extend `_FILLERS` with a
  one-line edit + a test, like `commands._UNIT_ALIASES`.
- **Command layer runs before the router.** `daemon._process` calls `handle_command`
  after transcribe; in button mode the system key's utterance is parsed as a command
  (or broadcast), executed by vupai and never injected. A non-command falls through
  to route+inject (never swallowed as `unknown`). Interpretation (`parse_command`) is
  separate from execution (`execute_command`); the `Command` dataclass is the seam for
  a future local-LLM interpreter (rules-first, deferred, not built).
- **Slash commands** (`slash_commands` config map, default `clear`->`/clear`,
  `compact`->`/compact`): grammar is `<verb> [name|all]` (verb leads, matching
  focus/close/swap). Bare verb -> focused pane, a name -> that pane, "all"/"everyone"
  -> every named pane. Unlike broadcast, the literal slash string is injected (not the
  spoken word). The slash-all path is "clear **all**"; the `broadcast_word`-leads path
  ("everyone clear") stays verbatim dictation, so they don't collide. Slash verbs must
  not shadow reserved verbs (create/close/focus/swap/zoom). **Unvalidated on a live
  daemon:** Claude's `/` autocomplete overlay may make the injector's `capture-pane`
  confirm-poll behave differently or have Enter pick a menu item; verify before trust.
- **ASR mishearing aliases (curated, per-token).** "pane" and the command verbs
  mishear as near-homophones; each is recovered by an explicit alias set in
  `commands.py`, same one-line-edit-plus-a-test pattern as `_FILLERS`. Scoring is
  deliberately avoided (it over-matches real words like "plans"/"lanes"), so the
  sets list only known mis-transcriptions and OMIT real-word lookalikes. Current
  sets: `_UNIT_ALIASES` (pain/pen/paint -> pane; windo* -> window), `_CREATE_VERB_ALIASES`
  (ate/hate/eight/crate/creator), `_CLOSE_VERB_ALIASES` (clothes/cloze/rose),
  `_SWAP_VERB_ALIASES` (swab/swamp), `_ZOOM_VERB_ALIASES` (zoo). **Precision over
  recall, scaled to blast radius:** destructive verbs keep tighter sets, and every
  alias is safe by construction because the parse still requires its operands (create
  needs a valid count, 1..`MAX_CREATE_COUNT` == 30; close needs a target; swap needs
  two names) - a misfire with no valid operand returns None and **falls through to
  verbatim inject**, never an action.
  `focus`/`kill`/`unzoom` are intentionally un-aliased (no clean real-word homophone).
  The `create` unit noun is also **optional** ("create two" == "create two panes",
  default pane) with homophone-free synonyms `agent(s)`/`split(s)` -> pane.
- **Large-create confirmation**: a `create` whose count reaches
  `config.confirm_create_threshold` (default 8) goes through the same `popup_confirm`
  y/n gate as the destructive kinds (`daemon._needs_confirm`), summarized as "open N
  panes". It shares the `confirm_destructive` master switch and `confirm_timeout_s`.
  Rationale: counts now run to 30 and `CALLSIGNS` was grown past that so every pane
  still auto-names, but tiling many panes is cramped and voice-addressing degrades
  past ~16 distinct names (`name_collides` even rejects near-sounding ones), so a big
  fan-out is worth a confirm. Spoken counts past nine come from `_NUMBER_WORDS`
  (ten..twenty, thirty); 21..29 rely on the digit transcription. Router number-routing
  (`_number`) stays capped at 1..9 regardless.

## Design decisions (settled rationale)

Hybrid routing (focus default + leading-name override) · push-to-talk, hold
Right-Option, no wake word · voice input only (TTS deferred) · Python daemon (not
Swift/native, not a browser app) · Parakeet via `parakeet-mlx` · **v1 drives Claude
Code panes only** (Codex/OpenCode have known send-keys submit bugs) · no control
word (the system key signals a command); `broadcast_word` is configurable ·
**agent-first**: both the session's initial pane (`ensure_up`) and created panes
default to `pane_command` ("claude"). Any program not on PATH degrades to a plain
shell — both paths check `shutil.which` (a command that exits at once would kill the
session / leave a dead pane): `ensure_up` warns, `_exec_create` appends a note to
its feedback. The agent runs **inside a shell** (`commands.wrap_agent_command`:
`<prog>; exec ${SHELL:-/bin/sh} -i`, passed as a single tmux command arg so tmux's
shell runs it) — so exiting the agent (e.g. claude's `exit`) drops the pane to an
interactive shell instead of closing it, leaving a usable terminal. The empty
plain-shell default is passed through unwrapped. Non-default programs are selected
by voice via the `programs` map
("create two shell/codex panes") — a data-driven token, **not** a new verb ·
multi-pane create
tiles the window · addressing mode is configurable (two-key
`button` default vs legacy single-key `keyword`, which has no command layer); the
dictation key keeps `alt_r` (muscle memory) and the system key defaults to `cmd_r`.

## Conventions

- Spec/plan docs under `docs/superpowers/` (and `.superpowers/` SDD scratch) are
  **local-only - never commit them** (gitignored). When a skill says "commit the
  design/plan doc," skip that step in this repo.
- Code comments in English. TDD with pytest; frequent small commits.
- Conventional commit messages, **no Claude attribution / co-authored-by lines**.
  Never push to `master` without asking.
- **Adding a `Config` field:** also add its doc block + commented default to
  `ANNOTATED_TEMPLATE` in `config.py` (a scalar `# key = default` line, or a
  commented `[table]` / array block for dict/set fields). The guard test
  `test_template_covers_every_config_field` fails CI if a field is missing, so
  the generated `config.toml` and `vupai config --init` stay complete. Scalar
  keys that `setup` should uncomment flow through `render_config` /
  `_merge_scalar_keys`.

## Known limitations / deferred

- **ASR name biasing is a no-op** - the installed `parakeet-mlx` `transcribe()` has
  no `hotwords` kwarg, so `asr.py` forwards-then-falls-back. Router name-matching is
  unaffected. For real biasing, swap the `Transcriber` to whisper.cpp/faster-whisper
  with `initial_prompt` (the Protocol keeps this contained).
- `router.route`'s `ambiguity_margin` is hardcoded (5); not exposed in `Config`.
- `tests/fixtures/tiny.wav` is absent → the `@slow` smoke test self-skips.
- Creating windows by voice is not yet supported (panes only).
- A macro reliably supports a single `create` plus `tile` (multiple creates in one
  macro share one registry snapshot, so only the first create can be named correctly).
- Local-LLM interpreter for `unknown` commands is deferred; the `Command` dataclass
  is the stable seam for that future escalation path.
