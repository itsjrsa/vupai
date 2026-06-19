# vtmux — voice control for tmux agent panes

Push-to-talk voice control over a tmux-based multi-agent workflow on macOS.
Hold a hotkey, speak, and the transcript is injected into the right tmux pane —
either the focused one, or an agent addressed by name ("Nova, run the tests").

## Status

Design phase — no code yet. Full design spec lives at
`docs/superpowers/specs/2026-06-19-voice-tmux-control-design.md`
(**local-only**, see Conventions).

## Locked decisions (do not re-litigate)

- **Hybrid routing**: speech → focused pane by default; a leading agent-name overrides.
- **Push-to-talk**, hold-to-talk (hold **Right-Option**). No wake word.
- **Voice input only** for v1; TTS deferred.
- **Python daemon** — not Swift/native, not a browser app.
- **ASR**: `parakeet-mlx` running `parakeet-tdt-0.6b-v3`, kept warm/resident.
- **v1 targets Claude Code panes only** (Codex/OpenCode have known send-keys submit bugs).
- **Startup**: `vtmux` wrapper boots tmux + a visible `🎙 voice` daemon window + attaches.

## Architecture

Single local daemon, small modules behind narrow interfaces:

```
hotkey → recorder → asr → router → injector → feedback   (+ tmux pane registry)
```

Talks to tmux via the `tmux` CLI; the hotkey is global (`pynput`), so the daemon
never owns the terminal. See the spec for component interfaces and data flow.

**Critical injection rule:** paste via `tmux paste-buffer -p`, then **poll
`capture-pane` until the pasted text appears before sending Enter** (avoids the
submit race). Keep tmux `extended-keys` *off* for the injector; target immutable
`pane_id` (`%N`), never a shiftable index.

## Stack

Python ≥ 3.11 · `parakeet-mlx` · `pynput` · `rapidfuzz` · `Metaphone` · `sox` · `tmux`.
macOS 26 (Darwin 25), Apple Silicon. Requires **Accessibility + Input-Monitoring +
Microphone** permissions granted to the terminal app (silent-fails otherwise).

## Conventions

- Spec/plan docs under `docs/superpowers/` are **local-only — never commit them**
  (gitignored). Keep them on disk for reference.
- Code comments in English.
- TDD: tests first — especially the router cascade (exact → rapidfuzz → metaphone),
  the `list-panes` registry parser, and the injector's poll-then-Enter loop.

## Commands

None yet (no code). Added as the implementation lands.
