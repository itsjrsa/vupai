# vupai - ideas & backlog

Loose, unprioritized backlog of potential directions. Not commitments. Items
marked _(deferred)_ are already called out in `CLAUDE.md` under "Known
limitations / deferred".

## High priority (global review, 2026-06-21)

Findings from a multi-agent audit (bugs adversarially verified). The confirmed
**bugs** below were FIXED in code; the two **verify-on-hardware** items and the
**MVP gaps / improvements** are open work, ordered by leverage.

### Bugs fixed (this pass)

- Number routing was off-by-one vs tmux's default `pane-base-index` 0 ("two" hit
  the 3rd pane, a silent wrong-pane inject). Routing is now positional within the
  focused window; `ensure_up` also pins `base-index`/`pane-base-index` to 1.
- Recorded WAVs leaked into `$TMPDIR` forever (one per utterance). `_process` now
  unlinks the source wav after journaling.
- Stale pidfile + PID reuse could SIGTERM an unrelated process or silently skip
  spawn. `_daemon_running`/`down` now verify the PID is really a vupai daemon.
- `vupai down` killed the daemon with no SIGTERM handler, orphaning the `sox`
  child and leaving `stop()`/`_SHUTDOWN` dead. Handler added; `run()` reaps the
  recorder on exit; `recorder.stop()` force-kills `sox` if SIGINT doesn't take.
- Inside tmux, `setup` named the app to grant as "tmux" (TCC attaches to the real
  terminal). `terminal_app` now ignores `TERM_PROGRAM=tmux/screen`.
- `focus`/`swap`/`zoom` didn't strip a leading "the" (unlike `close`/slash);
  "focus the nova" misrouted. Now consistent.
- Injector retry could double-paste a late-landing first paste, then submit it
  twice. Retry now skips the re-paste when the text already landed.
- `audio.list_input_devices` could crash on valid-but-non-object JSON; now guarded.

### Verify on real hardware, then fix

- **Spawn guard for in-pane `vupai reload`.** `_spawn_daemon` relies on
  `start_new_session` (setsid), which does NOT change the macOS TCC
  responsible-process. Running the documented in-pane `vupai reload` may parent
  the daemon under the tmux server, silently killing the global hotkey (the exact
  failure CLAUDE.md's "daemon must run OUTSIDE tmux" invariant warns about). NOT
  changed yet because it conflicts with the documented dogfooding loop and can't
  be verified in a unit-only env. Confirm responsible-process inheritance on a
  real Mac; if it reproduces, refuse to spawn from inside tmux (or re-exec under a
  non-tmux parent) instead of spawning a dead-hotkey daemon.
- **Injector confirm-poll false-positive on pre-existing text.** `_paste_and_poll`
  matches the needle anywhere on screen with no pre-paste baseline, so an utterance
  whose tail already appears on screen confirms instantly (the "poll until it
  lands" guarantee degrades to "is it somewhere on screen"). Disputed on impact
  (tmux may serialize paste-before-capture). Fix needs a pre-paste baseline +
  count-increase check and a full rewrite of the injector test fakes; defer until
  validated against a real Claude pane (where the `/`-autocomplete overlay also
  needs checking).

### MVP gaps (open, highest impact first)

- **Agent-state feedback / close the loop.** vupai is input-only; nothing pings
  back when an agent finishes, blocks on a permission prompt, or awaits input.
  Poll `capture-pane` per named pane, detect the idle/awaiting transition, fire a
  macOS notification + chime. (Overlaps the "Idle / done detection" item below.)
- **Confirmation/undo for destructive voice commands.** `close` / `close others`
  / broadcast fire on one ASR transcript with no confirm or undo, despite the
  project distrusting Parakeet's verbs (alias tables incl. real words like "rose").
  Config-gated confirm step for destructive kinds.
- **"Warming" indicator.** `run()` calls `warm()` (multi-second, or a multi-minute
  first-run download) BEFORE starting the listener/painting idle, so the daemon
  looks dead with no feedback. Paint a "warming" state before `Daemon.run()`.
- **Status distinguishes warming / ready / crashed.** A crashed daemon leaves the
  last-painted (often green) status frozen; `vupai status` prints "running"
  identically for warming and ready. Write a readiness marker after `warm()` and a
  stopped marker on exit.
- **Mic disconnect gives a clear, repeatable message.** A device lost mid-session
  hits the empty-capture path, which blames Microphone permission (wrong) and
  shows the hint only once. Reword to cover both causes and repeat it.
- **Command discoverability / live transcript HUD.** Show what was heard before/at
  inject; surface rejections on the focused pane, not just the truncated status
  segment. (Overlaps "Live transcript HUD" below.)

### Improvements (open)

- **Codify the convention-only invariants as tests.** warm()+transcribe() same
  thread (MLX), no inject/tmux/MLX on the listener thread, recorder timeout
  behavior. A refactor could reintroduce the `no Stream(gpu,0)` crash with all
  tests green. Drive on_press/on_release through real threads and assert thread
  identity + that the listener never touches inject/tmux.
- **Chatty `down`/`up`/`reload`.** They return 0 silently; the constant reload
  loop gives no signal. Print stopped/started/reloaded with pid.
- **Split `cli.py` (826 lines).** Extract the interactive `setup`/`_prompt_*`
  cluster (and optionally pidfile+lifecycle) into their own modules; they already
  take injectable collaborators.
- **Richer injection-failure feedback.** Name the target pane, point at the
  journal, log the last `capture-pane` snapshot so a transient miss is
  distinguishable from a systematic TUI incompatibility.
- **Honest mic-probe caveat.** doctor prints a confident `microphone=True` even
  though the probe can't tell "granted" from "denied-but-silent"; add a caveat
  line on all-passed.

## Closing the loop (vupai is input-only today)

- **Audio / TTS feedback.** Confirmation chime on successful inject, a distinct
  error tone, and optional spoken read-back of an agent's last output line. TTS
  is deliberately deferred for v1; this is the natural v2 frontier.
- **Idle / done detection.** Poll `capture-pane` to notice when an agent
  finishes or is blocked waiting for input, then notify (sound, pane
  `display-message`, or a macOS notification). Turns "talk at agents" into
  "agents ping you back."

## ASR quality

- **Real name biasing.** _(deferred)_ Swap the `Transcriber` Protocol to
  whisper.cpp / faster-whisper with `initial_prompt` seeded from active pane
  names + control words. The current `parakeet-mlx` `hotwords` path is a no-op.
- **Dictation correction verbs.** "scratch that" / "correct that" to clear or
  re-edit the pending paste before Enter is sent.
- **Streaming / partial transcription** for faster perceived latency.

## Command layer

- **Local-LLM interpreter for `unknown` utterances.** _(deferred)_ The `Command`
  dataclass is already the stable seam; rules-first, escalate only on `unknown`.
- **User-defined voice macros / aliases** in config (named layouts, command
  sequences, project-specific shortcuts).
- **Voice-driven window creation.** _(deferred)_ Currently panes only.
- **Multi-create macro with correct per-pane naming.** _(deferred)_ Today only
  the first `create` in a macro names correctly (shared registry snapshot).

## Safety / UX

- **Confirmation mode for destructive commands** ("clear all", close panes).
- **Undo / repeat last command.**
- **Live transcript HUD** so you can see what was heard before it's injected.

## Platform reach

- **Codex / OpenCode pane support.** _(deferred)_ v1 is Claude-Code-only by
  design, pending those tools' send-keys submit bugs.
- **Linux (Ubuntu) support.** Architecture is portable (tmux-CLI core, injected
  collaborators, `Transcriber` Protocol seam); two real blockers + one runtime
  risk. Scoped tasks:
  - **ASR — primary blocker.** `parakeet-mlx` requires MLX (Apple-Silicon Metal);
    it won't install on Linux. Add platform markers to `pyproject.toml`
    (`parakeet-mlx; sys_platform == 'darwin'`,
    `faster-whisper; sys_platform == 'linux'`) and ship a `faster-whisper`
    `Transcriber` impl behind the existing Protocol (asr.py:15-18). Bonus: its
    `initial_prompt` closes the "real name biasing" deferred item above.
  - **Permissions — UX blocker, not functional.** Linux has no TCC, so
    pynput/sox/tmux just work; but `permissions.py` (AXIsProcessTrusted via
    pyobjc, `open x-apple.systempreferences:` deep-links) and `cli.py`
    (`brew install`, `tccutil reset`) print misleading macOS advice. Guard with
    `sys.platform != "darwin"`: no-op the TCC probes, swap `open`->`xdg-open`,
    `brew`->`apt`.
  - **Wayland hotkey risk — must test on real hardware.** pynput's global
    listener wants X11; Wayland may silently eat key events (analogous to the
    macOS Input-Monitoring gotcha). X11 sessions expected fine. Verify before
    promising support.
  - recorder (`sox rec`), tmuxio, injector are already portable (only the
    install hint differs).

## Polish / infra

- **Expose `ambiguity_margin` in `Config`** (currently hardcoded to 5 in
  `router.route`).
- **Add `tests/fixtures/tiny.wav`** so the `@slow` smoke test runs instead of
  self-skipping.
- **Recognition-accuracy metrics / logging** to tune routing thresholds against
  real usage.
- **Journal-driven refinement loop.** The journal is the data source for
  continuously improving STT/routing/feature coverage. **Settled design
  decision: there is NO built-in analyzer** (no `vupai journal --analyze`, no
  LLM in the tool). You point whatever agent you want (Claude, OpenCode,
  self-hosted) at the raw `~/.config/vupai/journal.jsonl` per run and reason
  about improvements in that session. This sidesteps the "where does analysis
  run / who sees my voice data" question: you pick the tool each time. The build
  work is only to keep the journal maximally analyzable. **Shipped:** passive
  per-entry enrichment (`v`, ms `ts`, `model_id`, route `confidence` /
  `match_method` / `available_names`, `transcribe_ms` / `inject_ms`) so an
  external agent can spot fragile matches, STT mishears, and friction outcomes,
  and cluster rapid re-utterances (the misfire proxy) itself from ms timestamps
  + transcripts. **Possible next passes (all deferred):** `runner_up` + score on
  fuzzy matches; token-level STT confidence (needs a different `Transcriber`);
  `inject_ms` on the both-injects-fail path; a metaphone-path test assertion.
  Resist rebuilding this as an in-tool analyzer; the external-agent workflow is
  intentional.
