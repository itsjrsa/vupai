# voxpane - ideas & backlog

Loose, unprioritized backlog of potential directions. Not commitments. Items
marked _(deferred)_ are already called out in `CLAUDE.md` under "Known
limitations / deferred".

## Closing the loop (voxpane is input-only today)

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
