# vupai - voice control for tmux agent panes

Push-to-talk voice control over a tmux-based multi-agent workflow on macOS.
Hold a hotkey, speak, and the transcript is injected into the right tmux pane:
the focused one by default, or an agent addressed by name ("Nova, run the tests").

> This file is the single source of truth for all AI coding agents (Claude Code,
> Codex, opencode, Cursor, Aider, …). `CLAUDE.md` is a stub that points here.

## Status

v1 implemented and on `master` (600+ unit tests pass, `ruff` clean). Validated by
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
uv run python scripts/check_voice.py                 # type/debug voice actions w/o mic or daemon (-h for flags)
vupai doctor                                         # check macOS permissions, print fix steps
vupai setup                                          # interactive: probe + deep-link each missing-permission pane
```

`vupai` CLI (entry point `vupai.cli:main`):
- **vupai runs on its OWN tmux server** (a dedicated socket `tmux -L <tmux_socket>`, default `vupai`), so it never mutates the user's *default* tmux server. The socket is seeded **once** at the top of `main` by `_seed_tmux_socket` (env `VTMUX_TMUX_SOCKET` wins over `config.tmux_socket`; `""` opts back into the shared default server; name restricted to `[A-Za-z0-9._-]`). `tmuxio._base_argv` reads it live, so it propagates to: the detached daemon (Popen inherits `os.environ`), the `_daemon`/`_board` re-entry children, and tmux `run-shell`/`split` children, which do NOT inherit env and so get an explicit `VTMUX_TMUX_SOCKET=...` prefix from `tmuxio.socket_env_prefix` baked into `cli._self_cmd` / `board._self_cmd`. Net: every `set -g` / `set-hook -g` / `bind-key` lands only on vupai's server. (tmux still sources the user's `~/.tmux.conf` on vupai's server at startup, vupai just never *writes* to the default server.)
- **Sessions follow tmux-style verbs** (the session name is always a positional *after* a verb, never bare, so a mistyped subcommand errors instead of silently creating a session). The name defaults to the cwd basename, slugified by `_slugify_session` (tmux forbids `.`/`:`, so `my.app` -> `my-app`) via `_resolve_session_name`; each directory thus gets its own session. The daemon and vupai's tmux server are **global/server-wide within vupai's socket** (routing uses `list-panes -a`, callsigns unique across the server), so one daemon serves all vupai sessions and never sees the user's unrelated sessions. `ensure_up(name)` creates the session with `-c <cwd>` only when `has_session` is false, then returns the resolved name. `_enter_session` is **three-way** (`tmuxio.inside_vupai_server` via the `$TMUX` socket basename): not in tmux -> `attach`; inside vupai's own server -> `switch-client` (hop sessions); inside the user's **other** tmux -> cross-socket `attach` (`tmuxio.attach` clears `$TMUX` so the nested attach is allowed). `ensure_up` also enables `set-titles` with `set-titles-string "vupai - #S"` (`tmuxio.set_terminal_title`), so the terminal tab reads `vupai - <session>` instead of the bare launch command. On first run under a dedicated socket `_maybe_migration_notice` does a one-shot read-only probe of the default server (`tmuxio.default_server_footprint`, keyed on a `pane-border-format` carrying `@vupai_name`) and points the user at `vupai cleanup` if an older shared-server install left state behind.
  - `vupai [--reload]` (default, no subcommand) - attach-or-create the **cwd-named** session. `--reload` respawns the daemon first (= `vupai reload && vupai`) so source edits load before attaching - the dogfooding loop. **Inside vupai's own server it skips the attach** (`tmuxio.inside_vupai_server()`): same-server `tmux attach` refuses to nest, so `_cmd_default` respawns the daemon, prints a note, and returns - `--reload` from within degrades to a plain `reload`. From a normal shell or the user's *other* tmux it attaches (cross-socket) into vupai's server. Use bare `vupai reload` (no attach) as the in-pane dogfooding loop.
  - `vupai attach [NAME]` (alias `a`) - attach to NAME, creating it if absent (`tmux new -A -s NAME`). `_cmd_attach`.
  - `vupai new [NAME]` - create NAME, erroring if it already exists (`tmux new -s NAME`); then attach. `_cmd_new`.
  - `vupai kill [NAME]` - `kill-session -t =NAME`; the global daemon keeps running. `_cmd_kill`.
- `vupai up [NAME]` / `vupai down` - ensure a session + daemon without attaching / stop the daemon (`down` SIGTERMs the recorded pid; the daemon is a detached process, not a tmux window, and is global so `down` is not session-scoped)
- `vupai reload` - `down` + `ensure_up` in one step; respawns the daemon so source edits take effect (the daemon loads modules once at spawn, so a live one runs stale code). For dogfooding vupai on itself (or `vupai --reload` to also re-attach)
- `vupai cleanup` - revert vupai's leftover globals on the user's **default** tmux server (migration aid for installs that predate the dedicated socket). `tmuxio.cleanup_default_server` clears `VTMUX_TMUX_SOCKET` so the reverts hit the default server (never vupai's), then `revert_user_globals` restores the captured status-line originals, unsets the always-on globals (`pane-border-*`, `set-titles*`, `*base-index`, `extended-keys`, `status-*-length`), drops the autoname hooks + `<prefix>+R`, and clears the `@vupai_*` options. Every step swallows `TmuxError` so a down/clean server is a no-op. Caveat: the always-on globals revert to tmux's compiled defaults, not the user's `~/.tmux.conf` values (vupai never captured those).
- `vupai name <name> [pane]` - label a pane (rejects confusable names; defaults to focused)
- `vupai autoname [pane]` - assign the next free callsign from the pool to a pane unless already named; driven by the tmux pane-creation hooks (also usable by hand). `<prefix>+R` renames the active pane via this path's sibling `vupai name`
- `vupai status` - list panes **grouped by session** (focused session first, tagged `(focused)`; `*` = the single voice-target pane, `+` = each session's own tmux-active pane), daemon pid + log path, permission state
- `vupai ls` - lightweight, tmux-`ls`-style **session list** (one line per session on vupai's server): voice-focused session first and marked `*`, then alphabetical, with per-session `N agents/M panes` counts (agent = a pane with a `@vupai_name`) and `attached`/`detached` (any tmux client attached). Prints `no vupai sessions` (rc 0) when the server is down or empty. Distinct from `status`: no daemon/perm/board detail. `tmuxio.list_sessions` returns `(name, attached)` pairs and swallows the server-down `TmuxError` to `[]`. `_cmd_ls`
- `vupai mic [index|name|default] [--force]` - no arg lists CoreAudio **input** devices (marks the system default and the current pin); an index/exact-name pins that device into `config.toml` via `config.set_mic_device` (a merge writer that preserves comments/other keys), the literal `default` clears the pin. Before pinning a named device it runs `audio.probe_capture` (a 0.4s `rec` with `AUDIODEV` set) and **refuses an unusable device** (exit 1) rather than letting it silently yield "no audio captured" at speech time; `--force` pins anyway, and `default` (clearing the pin) skips the probe. The probe catches the failure mode where a USB mic exposes a speaker and a mic under the **same CoreAudio name** and sox's `AUDIODEV` name-match grabs the output (`can not get audio device properties`), plus disconnected/muted inputs and missing Microphone permission. Selection persists; a running daemon needs `vupai reload` to apply it. Enumeration is `audio.list_input_devices` (`system_profiler -json SPAudioDataType`, ~1s).
- `vupai keys` - no-arg, interactive: prints the current addressing mode + push-to-talk key(s), then runs the re-runnable picker (`_prompt_hotkey_setup`). Choose the addressing mode (`button`/`keyword`), then build a **list** of dictation keys (and, in button mode, command keys) via `_select_ptt_keys`: toggle keys into a fresh selection from a curated menu (`hotkey.PTT_KEYS`), by exact pynput key name, or by **pressing the key** (`p` → `hotkey.capture_key`, a one-shot pynput listen); `done`/bare Enter finishes and an empty selection keeps the current keys. Any one key in a list triggers that action (aliases for swapping keyboards). Button mode **refuses a key shared by both actions** (the command picker excludes the dictation keys). Persists `addressing`/`hotkey`/`command_hotkey` (the latter two as TOML arrays) via `config.set_hotkey_config` (a merge writer preserving comments/other keys); a running daemon needs `vupai reload` to apply. NOTE: macOS pynput collapses the left modifiers (`alt_l`→`alt`, `cmd_l`→`cmd`, `ctrl_l`→`ctrl`), so capture reports those bare names and `PTT_KEYS` uses them for the left-side entries.
- `vupai config --init` - ensure `~/.config/vupai/config.toml` lists every available key. When the file is absent it writes the full annotated template (every `Config` field present, commented at its default, with doc prose). When it exists, it **appends only the keys the file is missing** (as commented defaults under a labeled separator) and **never rewrites or reorders existing content** - so hand edits and chosen values are preserved and nothing is backed up because nothing is overwritten. Safe to re-run after an upgrade to top up newly added settings (`config.update_config`). First-run `vupai setup` writes the same annotated file (with the chosen journal/mic/hotkey keys uncommented).
- `vupai hosts` - list machines configured in `~/.config/vupai/hosts.toml` (name, host, user, port, program). `hosts.toml` is a separate file from `config.toml`; each `[hosts.<name>]` table requires `host` (hostname or IP) and optionally `user`, `port`, and `program`. **`program` is opt-in:** unset (or `""`) means "just open a login shell" (the default - you land at a prompt and start an agent yourself, in the right project dir); a named program (e.g. `claude`) auto-starts that agent. The listing shows `(shell)` for the no-program default. The file is not written by `vupai config --init`.
- `vupai hosts --init` - write a commented `hosts.toml` template to `~/.config/vupai/hosts.toml`; non-destructive (skips if the file already exists). SSH key auth must be configured separately (`ssh-copy-id`, `~/.ssh/config`, etc.).
- `vupai setup` - interactive permission bootstrap: detects the terminal app from `TERM_PROGRAM`, probes each permission (which triggers the macOS prompts), then `open`s the exact Settings deep-link pane for any that are missing and prints the `tccutil reset` recovery command. **Cannot grant on the user's behalf** - macOS TCC requires a human click; setup removes the navigation, not the consent. Deep-link/app-detect/open helpers live in `permissions.py` (`terminal_app`, `fixes`, `open_settings_pane`), injectable for tests. **First run only** (no `config.toml` yet), it also prompts for journaling consent (`journal_enabled` + `journal_keep_audio`) and writes the full annotated config via `config.write_full_config` (the chosen journal keys uncommented, every other key commented at its default); once a config file exists the prompt is skipped so re-running to confirm permissions never re-asks. It **always** runs a re-runnable mic-selection step (`_prompt_mic_setup`; bare Enter keeps the current device) **and a re-runnable hotkey step** (`_prompt_hotkey_setup`; bare Enter / non-interactive stdin keeps the current keys).
- `vupai board` - open a **supervision board**: a dedicated tmux pane (split right, ~40% width, off the focused pane) that summarizes, per named agent pane in the session, the main conclusion / pending action. Also a **spoken verb** (button mode): say "board" / "open board" / "show board" (`commands._parse_board` -> `kind="board"` -> `_exec_board`, sharing `board.open_board` with the CLI). `board_enabled` is reserved for a future auto-open on `vupai up`. The board is the foreground program of its own pane (the hidden `_board` loop), excludes itself (by `$TMUX_PANE`), is one-per-session (a second `vupai board` focuses the existing one via the `@vupai_board` tag rather than splitting again), and is torn down by closing the pane. Summaries are **edge-triggered** (only when a pane settles WORKING→IDLE), content-hash-gated, throttled per pane (`board_min_summary_interval`), and bounded to a scrollback tail, so token spend is bounded; `board_summarizer_cmd` (default `claude -p --model claude-haiku-4-5`) is swappable (`codex exec` / `gemini -p` / `ollama run …`) and degrades to a non-LLM last-line summary when absent or failing. See `board.py`, `panestate.py`, `summarize.py` and `docs/supervision-board-plan.md`.
- **`read [name]`** (spoken verb, button mode) - the **talk-back** path: vupai speaks a pane's summary aloud; also accepts an and-joined list of names ("read echo and sage") (`commands._parse_read` -> `kind="read"` -> `_exec_read`). Resolves a named pane (else the focused one), captures + bounds its tail, summarizes it into a few **spoken sentences** via `summarize.summarize_read` (richer than the board's one-liner, and grounded in the pane **title** - what the pane is about, fetched with `tmuxio.pane_title`), reusing `board_summarizer_cmd`, and reads it through `speech.speak` (`tts_cmd`, default `say`). `tts_enabled` gates only the audio; the summary always surfaces on the status line, so "read" stays useful silent. With `tts_stream` on (default) the real run takes the **streaming** path instead (`summarize.summarize_read_stream` -> `stream_run` reads the summarizer's stdout incrementally; `speech.SentenceSpeaker` speaks each sentence as it completes, waiting on each `say` so they never overlap, while later sentences are still generating) - first words out in ~1-2s. This pays off only with a streaming-capable `board_summarizer_cmd` - the **default** is the bundled `vupai.claude_summarize` (run as `sys.executable -m vupai.claude_summarize`, computed in `config._DEFAULT_SUMMARIZER`), which runs Haiku in stream-json mode and relays text deltas; `scripts/ollama_summarize.py` streams a local/remote Ollama. Swap in plain `claude -p` (buffers, speaks once at the end) to opt out. An injected `summarize_fn` (the unit suite) forces the original one-shot path. Streamed sentences route through the same mute-gated `daemon._speak` (now returning the `say` handle so the speaker serializes on it, and stored as `_last_ack` for barge-in). The single-pane streamed read is **length-capped** to `config.read_max_sentences` (default 2; the prompt asks for 1-2 sentences): `SentenceSpeaker` stops after N sentences and drops the rest, so a chatty model can't run long. `read board` is **not** capped (its length tracks the agent count). **Runs off the main thread** (`daemon._dispatch_read` -> `_async_fn`): the summary is slow and `say` blocks, so inlining it would stall the next utterance. The worker uses its **own `PaneRegistry`** (never `self._registry`, which the main loop refreshes) and the journal records the dispatch, not the spoken result. On-request only - there is no unprompted narration. Aliases: `reed` / `red` (reading is non-destructive, so the set is generous). **`read board`** (also `read all`, parsed as `to_all=True`) speaks a board-style **digest of every agent** instead of one pane: `board.collect_statuses` snapshots each named pane in the focused session (two captures a beat apart -> `ChurnClassifier` working/idle, refined by needs-input; one-line `summarize.summarize` per pane, concurrent), `_exec_read_board` excludes the board pane via `find_board_pane`, and `board.speak_statuses` renders "N agents on the board. nova, claude, working: ...". With `tts_stream` on it streams too (`_exec_read_board_stream`): the "N agents on the board." header is voiced immediately while summaries run, then `collect_statuses`' `on_status` hook feeds each agent's `board.status_clause` to the `SentenceSpeaker` in pane order as its summary lands (the per-pane summary is still the one-shot one-line `summarize.summarize`; only the speaking is incremental). It needs **no open board pane** - it computes the same data on demand - so there is no "open board then read board" chaining.
- **`ssh [host]` / `connect [host]`** (spoken verb, button mode) - opens one new pane and connects over SSH to a machine defined in `~/.config/vupai/hosts.toml`, fuzzy-matching the spoken host name. Runs a single wrapped `ssh -t` command (`commands.wrap_remote_command` + `wrap_agent_command`). **Default (no `program`): just a login shell** - you land at a remote prompt and start an agent yourself in the right project dir, rather than launching one in `$HOME`. When a host sets `program`, that agent is started on the remote **through a login+interactive shell** (`${SHELL:-/bin/sh} -lic '...'`) so the remote `PATH` (nvm/fnm/npm) loads - ssh's command mode otherwise uses a non-interactive, non-login shell where `claude` is "command not found". The agent is wrapped so exiting it drops to a remote shell, and the whole ssh is wrapped locally so the pane survives disconnect. We can't `which` a remote PATH, so the remote shell-wrap is also the missing-agent fallback. Border program label is `<program>@<host>` (or `ssh@<host>` for the shell default). SSH key auth must be set up beforehand. No confirm gate; intent ack "connecting to <host>", success ack "<callsign> is up".
- **Spoken command acks + `mute` / `unmute`** (button mode) - commands **speak feedback**, not just paint the status line. Talk-back is **curated**, not blanket - speak what you can't see, stay quiet when the screen already shows it, always speak failures - and two-phase so it feels immediate (the spoken ack used to trail the popup-gated, sometimes slow execution): (1) an **intent ack** `commands.intent_phrase(cmd)` (present tense - "closing sage", "opening an agent", "sending clear") is voiced by `daemon._process` the instant the command is recognized, **before** the confirm popup and execution, for the curated `_ANNOUNCE_INTENT = {create, close, close_others, broadcast, slash, board}` (off-screen / irreversible actions); the **view/navigation verbs (focus, zoom, unzoom, layout, swap) announce NOTHING on success** - the cursor jump / resize / re-tile is its own instant feedback. (2) a **result ack** after execute speaks **only on failure** ("no pane named sage" - the eyes-off case, voiced for EVERY kind incl. the view verbs) or for kinds whose success adds info (`_SPEAK_ON_SUCCESS = {create, talkback}` - a create's assigned callsign "sage is up", a toggle's confirmation), so an announced success is just the one immediate intent phrase. A confirm-cancelled destructive speaks "cancelled" after its intent. The say-friendly result phrase is `CommandResult.spoken` (set on exec paths whose status `message` is symbol-laden - `swapped a <-> b`, `sent /clear to nova`, `broadcast to 2/2 agents` - so the spoken twin drops the `<->`/`/`/`N/M`); empty `spoken` falls back to the word-only message. All voicing goes through `daemon._speak` (best-effort, non-blocking `speech.speak`). The **runtime mute** `self._talkback` (seeded from `config.tts_enabled`, the persisted default) is the **single master switch** over ALL talk-back (intent + result acks AND `read`), flipped live by the `mute`/`unmute` voice command (`commands._parse_talkback` -> `kind="talkback"`, `enable` bool; synonyms `quiet`/`be quiet`/`stop talking`/`shut up` off, `speak up`/`talk back`/`talk to me` on - matched on the whole utterance so common words never shadow a pane action, button key only). `daemon._process` flips the flag **before** running so unmute confirms aloud and mute goes silent at once. `read` routes its `speak_fn` through the same `self._speak`, so one toggle covers both. **Barge-in / `stop`**: pressing ANY push-to-talk key calls `daemon._silence` at the top of `_on_press` (sets the active read's cancel `Event` `self._read_cancel` and terminates the in-flight `say` handle `self._last_ack`), so a readback stops the instant you speak - and `say` audio never bleeds into the mic to pollute the transcript. A transient **`stop`** command (`commands._parse_stop` -> `kind="stop"`; synonyms `enough` / `that's enough` / `cancel` / `skip` / `that's all`, button key, whole-utterance match) silences in-flight talk-back via the same `_silence` WITHOUT flipping the persistent `_talkback` mute - it is deliberately **disjoint** from the `mute`/`unmute` word set (`_TRANSIENT_STOP` vs `_TALKBACK_OFF`). The cancel `Event` also propagates into `stream_run`, killing the streaming-summarizer subprocess so an interrupted read stops spending tokens.
- `vupai _daemon` / `vupai _board` - hidden; the long-running daemon process (spawned detached, logs to `~/.config/vupai/daemon.log`) and the in-pane supervision-board render loop

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
| `src/vupai/hotkey.py` (`MultiHotkey`) | one pynput listener over N PTT keys, each independently debounced. Button mode binds every dictation key to the dictation callbacks and every system key to the command callbacks; keyword mode binds all dictation keys. `_make_hotkey` falls back to keyword mode on overlap/empty/unknown keys |
| `src/vupai/recorder.py` | `sox rec` → wav, SIGINT to stop; exports `MIN_WAV_BYTES`; optional `device` → `AUDIODEV` env |
| `src/vupai/audio.py` | enumerate CoreAudio **input** devices (`system_profiler -json`); `resolve_device` (configured name → present? else fall back to default + warning) |
| `src/vupai/asr.py` | `parakeet-mlx` `Transcriber` Protocol, lazy `warm()` + cache |
| `src/vupai/router.py` | name cascade exact→rapidfuzz→metaphone, number-in-window, focus fallback, near-tie ambiguity; `CALLSIGNS` pool + `next_callsign` (auto-name picker) |
| `src/vupai/registry.py` | `Pane` (incl. `session` from `#{session_name}`) + `PaneRegistry` parsed from `tmux list-panes -a` |
| `src/vupai/injector.py` | paste → poll `capture-pane` → Enter (the safety core) |
| `src/vupai/tmuxio.py` | thin exact-argv wrappers over the `tmux` CLI |
| `src/vupai/feedback.py` | status to stdout / `display-message` on the target pane |
| `src/vupai/permissions.py` | best-effort macOS permission probes + `hints` |
| `src/vupai/config.py` | TOML config at `~/.config/vupai/config.toml` + defaults; `_FIELD_BLOCKS` (per-field doc + commented default) is the single source of truth, `ANNOTATED_TEMPLATE` is built from it and `render_config` uncomments chosen keys; `write_full_config` writes a fresh full annotated file (first-run `setup`; does NOT merge) and `update_config` additively appends only a file's missing key-blocks (for `vupai config --init`; never overwrites, no backup); `set_mic_device` / `set_hotkey_config` MERGE keys in place via shared `_merge_scalar_keys` (preserve comments/other keys, uncomment a commented default in place); `_format_toml` renders a value as a quoted scalar or, for `hotkey`/`command_hotkey`, a TOML array. `hotkey`/`command_hotkey` are normalized to deduped tuples by `Config.__post_init__` (accepting a bare string or a list), so every consumer sees the same shape |
| `src/vupai/panestate.py` | shared pane-state classification: the watcher's marker `classify_state` (re-exported by `watcher.py`) plus the board's tool-agnostic `ChurnClassifier` (content-churn baseline + optional per-tool `MARKERS` + settle/hysteresis) and a generic `detect_needs_input` |
| `src/vupai/summarize.py` | best-effort swappable pane summarizer: runs `board_summarizer_cmd` (no shell) with the prompt+tail as one argv arg, reads the last non-blank stdout line, degrades to a stdlib last-line fallback on any failure. `summarize_read` is the `read` command's richer variant: a read-specific prompt + the pane title, keeping the WHOLE reply (multi-sentence, spoken-length, sentence-boundary cap) instead of one line. To avoid the ~3s `claude -p` CLI cold-start per call, point `board_summarizer_cmd` at `scripts/ollama_summarize.py` (stdlib-only adapter -> a local/remote Ollama `/api/generate`, host/model via `--host`/`--model` or `OLLAMA_HOST`/`OLLAMA_MODEL`, `keep_alive=-1` to stay warm, silent non-zero exit on any failure so the fallback still fires) |
| `src/vupai/speech.py` | best-effort swappable text-to-speech sink (vupai's voice for both per-command acks and the `read` summary): runs `tts_cmd` (default `say`) with the phrase as one argv arg, **non-blocking** (`Popen`, never awaited - `say` blocks until done), swallows every failure; gated at the call site by the daemon's runtime `_talkback` switch (`mute`/`unmute`) |
| `src/vupai/board.py` | supervision-board engine: own `PaneRegistry` + Event poll loop (twin of `PaneWatcher`), per-pane edge/hash/throttle/in-flight gating, cold-start, self-exclusion, and a pure `render_frame` printed to the board pane's stdout; plus on-demand `collect_statuses` / `speak_statuses` for the spoken `read board` digest (same data, no polling thread) |
| `src/vupai/hosts.py` | machine inventory loader (`load_hosts`, `HOSTS_PATH`), host name slugifier, and fuzzy resolver (`resolve_host`) for the `ssh` spoken command |
| `src/vupai/commands.py` | parse control-word utterances into `Command`s and execute them (create/macro/focus/swap/close/zoom/layout/board/read/slash/broadcast/ssh); interpretation split from execution |
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
- **Focus is per-client; the daemon resolves to ONE pane.** `tmuxio.focused_pane_id()`
  picks the voice-target pane via the **most-recently-active attached client**
  (`list-clients` by `client_activity`, then `display-message -c <client>`), falling
  back to a bare `display-message` only when no client is attached. The daemon is
  detached with no client of its own, so a bare query resolves against the server's
  globally most-recent session - the WRONG repo on a multi-session server.
  `registry.focused()` returns that single pane. Note `Pane.active` (per-window
  `pane_active`) is True for many panes at once and is NOT focus; only `status` reads
  it (for the `+` marker).
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
  so units run with fakes - no real tmux/mic/model in the unit suite. `test_cli.py`'s
  hand-rolled `FakeTmux` (swapped in via `monkeypatch.setattr(cli, "tmuxio", …)`)
  needs a method for every new `tmuxio` function `cli.py`/`board.py` call, or unit
  tests `AttributeError`. `cli.main()` exports `VTMUX_TMUX_SOCKET` (and `attach()`
  clears `$TMUX`); `tests/conftest.py` autouse-restores both, so don't assume a
  clean env across `cli.main()` calls in tests.
- **Addressing mode (`addressing` config):** `button` (default) uses two keys:
  the `hotkey` (dictation) injects verbatim into the focused pane (no parse, no
  name routing), and the `command_hotkey` (system, default Right-Command) runs the
  command layer with `addressing="button"` (no control word; a non-command falls
  through to name/number routing, but an **unaddressed** utterance — one that hits
  the focus fallback — is rejected, not injected, so the system key never duplicates
  the dictation key's verbatim-to-focused write). `keyword` is the
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
  to the router and injects **only when it addresses a pane by name/number**; an
  unaddressed system-key utterance (focus fallback, `Route.fallback`) is rejected
  (`outcome="not_addressed"`), since verbatim-to-focused is the dictation key's job.
  `keyword` mode keeps the focus fallback (it has no command layer). Interpretation
  (`parse_command`) is
  separate from execution (`execute_command`); the `Command` dataclass is the seam for
  a future local-LLM interpreter (rules-first, deferred, not built).
- **Layout names are name-phrases, never lead verbs.** `layout <name>`
  (`commands.py` `_LAYOUTS`) requires the `layout`/`lay out` lead verb; the names
  (grid/left/top/columns/rows + aliases) are real English words and are safe only
  as the token(s) AFTER the verb. Never add a layout name to a `toks[0]`-matched
  set. Focus-aware main (`main-vertical`/`main-horizontal`) swaps the focused pane
  into the lowest-index "main" slot with `swap-pane -d` (keeps focus), then
  `select-layout` (which auto-unzooms). The `vupai voice-commands` cheat sheet
  (`cli.py` `_voice_commands_text`) lists `layout <name>` with the canonical names
  only (not aliases); keep that row in sync when the command set changes.
- **Bulk "all" ops are session-scoped.** The registry is server-wide (`list-panes -a`,
  callsigns unique across the server) so name-addressed commands route across repos -
  but `close` all/others, `broadcast`, and slash-to-`all` filter to the **focused pane's
  session** (`Pane.session`) so they can't kill/blast another repo's panes. Broadcast and
  slash-all require a focused pane to anchor the session (fail-safe: refuse, don't fan out).
  Targeted multi-pane ops use an and-joined name list ("close echo and sage", "clear echo
  and sage", "read echo and sage"): close/slash/read accept 2+ names (split on "and";
  commas collapse in tokenization) and run best-effort - each target resolves
  independently, hits act, misses are reported ("closed echo - no pane named ghost"). An
  all-target word anywhere in the list falls back to the existing all-path. focus/zoom/swap
  stay single-target; subset broadcast is deferred.
- **Slash commands** (`slash_commands` config map, default `clear`->`/clear`,
  `compact`->`/compact`): grammar is `<verb> [name|all]` (verb leads, matching
  focus/close/swap); also accepts an and-joined list of names ("clear echo and sage").
  Bare verb -> focused pane, a name -> that pane, "all"/"everyone"
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

### Status-bar tips (keep in sync)

Rotating discoverability tips render in tmux status-left, generated by
`tips.build_tips` (see `src/vupai/tips.py`). When you add a new spoken command,
slash verb, macro, or program, consider adding a matching example to
`build_tips`, and confirm with the user whether it belongs in the rotation
rather than adding it silently. The verb examples reuse the parser's verb
constants so they cannot drift from `commands.py`.

## Design decisions (settled rationale)

Hybrid routing (focus default + leading-name override) · push-to-talk, hold
Right-Option, no wake word · input-first, with on-request talk-back only (the
`read` command speaks a pane summary via `tts_cmd`; no unprompted TTS) · Python daemon (not
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
- **Manual voice debugging without the mic:** `scripts/check_voice.py` (tracked,
  not packaged) types an utterance through the same parse/route/inject path the
  daemon uses for each keybind (`system`/`dictation`), against the focused pane.
  `read` speaks unless `--silent`; `--dry` shows decisions with no side effects;
  `--pane`/`--summarizer`/`--tts-cmd`/`--config` make it a deterministic harness.
- Conventional commit messages, **no Claude attribution / co-authored-by lines**.
  Never push to `master` without asking.
- **Adding a `Config` field:** also add a `(name, block)` entry to
  `_FIELD_BLOCKS` in `config.py` (the block is the field's doc line(s) + its
  commented default: a scalar `# key = default`, or a commented `[table]` /
  array block for dict/set fields). `ANNOTATED_TEMPLATE` is built from
  `_FIELD_BLOCKS`, so the generated file, `vupai config --init` (which appends
  only missing blocks), and the drift guard `test_template_covers_every_config_field`
  (fails CI on a missing field) all stay complete automatically. Scalar keys
  that `setup` should uncomment flow through `render_config` /
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
- **Supervision board (`vupai board`) deferred items:** auto-open on `vupai up`
  (`board_enabled` is wired but unused), per-tool `MARKERS` beyond Claude (the
  churn baseline already covers other tools), configurable board geometry, a
  pane-border one-line summary as a second renderer, cross-session scope, and
  batching multiple panes into one summarizer prompt. The board reads config
  once at launch (reopen the pane to apply changes), like the daemon.
