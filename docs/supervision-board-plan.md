# Supervision board: design plan

Status: draft for review. No code written yet. Branch: `feat/supervision-board`.

## 1. Goal

When you run several tmux panes, each driving an agentic coding tool (Claude
Code, codex, gemini, aider, ...) on a different slice of a project, you lose the
thread of what each one is doing. The board is a dedicated tmux pane that shows,
per agent pane (by callsign), a one or two line summary of its **main
conclusion / pending action**, so you can supervise the whole fleet at a glance.

Hard constraints carried from prior discussion:

- **Tool-agnostic.** vupai already works with non-Claude agents. The board must
  not make vupai Claude-only. State detection works for any TUI agent; the
  summarizer is a swappable command; every LLM-touching path degrades to a
  non-LLM fallback.
- **Token cost is the #1 fear.** Summaries are **edge-triggered** on settled
  state transitions, never on a timer, and are gated by a content hash, a
  per-pane min-interval throttle, and an in-flight guard. Worst-case spend is
  provably bounded.
- **Stay small.** vupai is ~5k lines, four runtime deps, no frameworks. The
  board lands as ~3 small modules (~300 lines) that mirror existing
  `watcher.py` / `tips.py` idioms. No new runtime dependency (uses stdlib
  `difflib`, `subprocess`, `hashlib`).

## 2. What it looks like

A vertical split (right side by default) running a plain Python render loop that
owns the pane and prints to its own stdout:

```
 vupai board · myproj                              14:32

 ● nova    claude   working
     Refactoring auth module, running the test suite.

 ◌ atlas   codex    idle
     Done: added retry/backoff to the HTTP client.

 ◆ orion   aider    needs input
     Approve migration 0007 before continuing? (y/n)

 ◌ vega    claude   idle
     Concluded: API docs regenerated, 3 files changed.
```

- Glyphs: `●` working, `◌` idle, `◆` needs input. (Color optional, glyph is the
  load-bearing signal so it reads on any theme.)
- Row 1 per pane: `glyph  callsign  program  state`.
- Row 2 per pane: the short summary (<= ~100 chars), or `needs input` text.
- Header: `vupai board · <session>` and a clock.
- Only **named** panes in the **focused session** appear (callsign != pane id).
  The board excludes itself.

The board redraws only when a state or summary actually changes, so it does not
flicker.

## 3. Architecture: standalone process owning a dedicated pane

The board is **not** a daemon thread. It is a standalone process that **is** the
foreground program of its own tmux pane and writes the frame to its own stdout.

Why not a daemon thread (like the watcher)? Rendering into a *dedicated pane*
requires a process to be that pane's program. A daemon thread can't be a pane's
program, which would force rendering through `@vupai_*` options or `send-keys`,
both of which are wrong for multi-line per-pane content (tmux format strings
can't loop over panes or wrap paragraphs; send-keys races the pane's shell).
A standalone process prints `\033[2J\033[H` + frame and is done.

Consequences, all positive for our constraints:

- The user opens/closes the board pane at will; it is ephemeral and glanceable.
- A summarizer bug is isolated to one process; kill the pane and it's gone.
- Config is read at launch; "reload" is just reopening the pane (no daemon
  reload machinery).

Two CLI entry points (mirrors the existing hidden `_daemon` pattern):

- `vupai board` (public): resolves the focused session, splits a pane in it
  running `python -m vupai _board`, marks that pane, returns. Thin launcher.
- `vupai _board` (hidden): the render loop, running **inside** the new pane.

The loop is a structural twin of `watcher.PaneWatcher` / `tips.TipRotator`:
its own `PaneRegistry`, a `threading.Event`-interruptible poll at
`board_poll_interval`, exception-swallowing ticks, SIGTERM/SIGINT trap (same
idiom as `_cmd_daemon`), and best-effort `kill_pane(self)` on exit.

### Self-identification and exclusion

The renderer learns its own pane id from `$TMUX_PANE` (tmux sets this in every
pane, no plumbing needed) and excludes it from the poll set. To stop two boards
from summarizing each other, the launcher tags the board pane with
`@vupai_board=1` and `vupai board` refuses to open a second board in a session
(it focuses the existing one instead, found via the tag). If the board's own
pane id can't be resolved, it watches nothing rather than risk summarizing
itself.

## 4. Data flow per tick

```
                  every board_poll_interval (default 2.0s)
  registry.refresh()
        │
        ▼
  for each named sibling pane in focused session (excluding self):
        │
        ├─ tail = last board_capture_lines of capture_pane(pane.id),
        │         then truncate to board_tail_bytes (keep the tail of bytes)
        │
        ├─ churn = 1 - SequenceMatcher(prev_tail, tail).ratio()
        ├─ content_hash = blake2b(tail)
        │
        ├─ classify (churn + optional markers, hysteresis + settle streak)
        │        WORKING / IDLE / (IDLE->NEEDS_INPUT via heuristic)
        │
        └─ on WORKING -> IDLE(settled) edge:
                 if content_hash == last_summary_hash:        skip (no change)
                 elif now - last_summary_at < min_interval:   skip (throttle)
                 elif inflight[pane]:                         skip (in-flight)
                 elif low_information(tail):                  render deterministic, no LLM
                 else: dispatch summarize(tail) on a worker (cap board_max_concurrent)
        │
        ▼
  if any state/summary changed: re-render the frame to stdout
```

Cold-start: on launch, every currently-IDLE named pane is enqueued for one
summary pass (subject to the same hash / in-flight gates). This is the only time
summaries fire without a fresh edge.

## 5. State detection (tool-agnostic churn baseline + optional markers)

The watcher's `classify_state` only knows Claude Code's TUI markers
(`esc to interrupt`, `? for shortcuts`) and returns `UNKNOWN` for everything
else. Shipping that as the board's only signal would make the board Claude-only,
the exact lock-in we rejected. So the **baseline is content churn**, which works
for any TUI agent, and markers are an optional accelerator.

Per pane, per tick:

1. `churn = 1 - difflib.SequenceMatcher(None, prev_tail, tail).ratio()`
   (pure stdlib; n is bounded by the tail size, so cost is negligible at 2s).
2. Classify with a dead-band (hysteresis) so redraw flicker doesn't flap state:

   ```
   if churn >= churn_active (0.10):   raw = WORKING
   elif churn <= churn_idle (0.01):   raw = IDLE
   else:                              raw = prev_raw     # hold, no flap
   ```

3. **Settle** = `board_settle_ticks` (default 2 -> ~4s) consecutive IDLE
   observations after a WORKING run. Only then does the `WORKING -> IDLE` edge
   fire. A single quiet tick is not "settled" (a spinner can pause).

Optional marker refinement (never required, additive):

- A per-tool WORKING marker present -> force WORKING (covers a genuinely-working
  but not-yet-redrawn frame).
- A per-tool IDLE marker present -> settle in 1 tick instead of N (the tool told
  us it's done).
- Unknown tool / no markers -> pure churn path. Accuracy degrades gracefully;
  nothing breaks.

Markers are keyed by the program label vupai already stores in `@vupai_program`
(`set_pane_program`), so per-tool refinement needs zero new tmux plumbing. v1
ships the Claude marker set already in the codebase plus the churn baseline;
codex/gemini/aider marker sets are additive follow-ups.

### needs input (light, piggybacks on the summary)

`NEEDS_INPUT` is a cosmetic annotation, not a gate on anything (the watcher
already owns the load-bearing "ready for input" notification). Two sources, no
extra LLM call:

- If the summarizer line starts with `NEEDS: ` (the prompt asks for this) -> set
  NEEDS_INPUT, strip the prefix for display.
- No-LLM fallback path: a cheap generic regex on the settled tail's last line
  (`?$`, `y/n`, `[Y/n]`, `proceed?`, a trailing prompt glyph). High precision,
  low recall, which is fine for a cosmetic flag.

## 6. Summarization (swappable command, bounded, cost-guarded)

### Contract (the agnosticism-critical part)

`board_summarizer_cmd` is a config string (default `claude -p`). vupai resolves
it at use time with `shlex.split` (never `shell=True`), and invokes:

```
argv   = shlex.split(board_summarizer_cmd) + [PROMPT]
stdin  = none
stdout = captured -> sanitized
stderr = captured, logged at debug, never parsed
```

`PROMPT` is one string = a fixed tool-neutral instruction + the bounded
scrollback tail. Passing the whole prompt as the final argv arg (not stdin) is
the shape that `claude -p "<x>"`, `codex exec "<x>"`, `gemini -p "<x>"`, and
`ollama run <model> "<x>"` all accept unmodified. The bounded tail (<= ~6 KB) is
far under ARG_MAX, and with a list argv + no shell there is no injection or
quoting surface. (Validated against real `claude -p` during implementation; if a
given tool prefers stdin, that is documented, but the contract floor needs no
per-tool flag.)

Instruction text (tool-neutral, imperative, short):

> Summarize the state of this terminal pane for a supervision dashboard. Output
> ONE line, max 100 chars: the main conclusion or the pending action/question.
> No preamble, no markdown, no quotes. If the agent is waiting for input, start
> the line with `NEEDS: `.

### Reading the result (degrade, don't trust)

1. Read stdout to completion.
2. Take the **last non-blank line** of stdout. This single rule neutralizes
   tools that interleave an event trace or print a banner (codex does), and is a
   no-op for `claude -p` (one clean block). It is the agnostic lowest common
   denominator; no per-tool flag required.
3. Strip ANSI, collapse whitespace, truncate to `board_summary_max_chars`.
4. Leading `NEEDS: ` -> set NEEDS_INPUT, strip prefix.
5. Empty result -> treat as failure -> fallback.

### Graceful degradation

`FileNotFoundError` (command not on PATH), non-zero exit (auth error, etc.),
`TimeoutExpired`, or empty output all route to one pure-stdlib fallback:

```python
def fallback_summary(tail, needs_input):
    lines = [l for l in tail.splitlines() if l.strip()]
    if not lines: return "(no output)"
    last = lines[-1][:90]
    return f"NEEDS: {last}" if (needs_input or detect_needs_input(tail)) else last
```

The board always renders something: the LLM line when available, the last
meaningful line otherwise. A setup with no LLM CLI at all still gives a useful
glance view. The summarizer is best-effort exactly like `_osascript_notify`.

### Cost guards (this is what bounds spend)

| Guard | Default | Why |
|---|---|---|
| Edge-trigger on settled WORKING->IDLE | - | No timer-driven summaries. |
| Settle dwell (`board_settle_ticks`) | 2 (~4s) | Kills WORKING/IDLE flap, the dominant leak. |
| Content-hash gate | - | Unchanged tail -> no call, no redraw. |
| Min-interval throttle (`board_min_summary_interval`) | 30s | Hard floor: <= 120 summaries/pane/hour no matter what. |
| In-flight guard | - | Never two summaries for one pane at once. |
| Bounded tail (`board_capture_lines` + `board_tail_bytes`) | 40 lines / 6 KB | Input scales with what's on screen, not history. Byte cap stops one huge minified line. |
| Low-information pre-filter | - | If the settled tail is just a bare prompt / `? for shortcuts` footer, render `idle` deterministically and skip the LLM. A large fraction of edges have nothing to summarize. |
| Output cap (`board_summary_max_tokens`, if the CLI exposes it) | ~96 | Output is 5x input price; a board cell is ~2 lines. |
| Concurrency cap (`board_max_concurrent`) | 2 | Bounds the cold-start / layout-change herd. |

Worst case, N=6 panes, Haiku: the 30s throttle floor caps it at ~720 calls/hr ~
1.7M tokens/hr ~ **$2/hr** absolute. Realistic duty cycle (a pane finishes every
~5 min) is ~72 calls/hr ~ **$0.19/hr**. On a high-tier default model multiply by
~5-7x, which is why the **model default is the biggest lever** (see open
decisions).

## 7. Rendering

A pure function `render_frame(tracks, session, now) -> str` builds the ANSI
frame; the loop diffs against the last frame and only writes on change
(`\033[2J\033[H` + frame). Pure function = snapshot-testable without a terminal.

Pane geometry in v1: one `split_window` (right, ~40% width) plus optionally one
`select_layout`. No configurable geometry, no resize handling. The pane is the
board; if the user resizes or closes it, the process adapts or dies with it.

## 8. Lifecycle

- `vupai board`: `PaneRegistry().focused()` -> session; `split_window(target,
  "python -m vupai _board", horizontal=True, size="40%")` -> new pane id (right
  vertical split, ~40% width); `mark_board_pane(id)`; return 0. v1 is
  manual-launch only; `board_enabled` is reserved for a future auto-open on
  `vupai up`.
- `vupai _board`: read `$TMUX_PANE`; build own registry; install SIGTERM/SIGINT
  trap that sets the stop Event; run the poll/summarize/render loop; on exit,
  best-effort `kill_pane(self)`; restore signal handlers in `finally` (so the
  test suite's signal state isn't polluted, per the existing gotcha).
- Closing the pane manually kills the process (its stdout pane is gone). Clean.

## 9. Files

New:

- `src/vupai/panestate.py` - extract `PaneState`, `classify_state`,
  `_WORKING_MARKERS`, `_IDLE_MARKERS` from `watcher.py`; add `NEEDS_INPUT` to
  the enum; add the churn classifier (`ChurnClassifier` / `classify_churn`) and
  a `MARKERS` table keyed by program label. `watcher.py` re-imports
  `PaneState, classify_state` so `test_watcher.py` stays green with zero edits
  (pure move + re-export).
- `src/vupai/summarize.py` - one function plus the fallback (~50 lines). Mirrors
  `_osascript_notify`'s best-effort swallow. No provider abstraction, no
  per-tool prompt templates.
- `src/vupai/board.py` - the `Board` class (own registry, Event loop, per-pane
  `PaneTrack`, churn classify, edge/settle, hash gate, throttle, in-flight,
  cold-start, dispatch) + the pure `render_frame`. ~150-200 lines.

Modified:

- `src/vupai/cli.py` - `_cmd_board` (public launcher) + hidden `_board`
  (registered in `sub._name_parser_map` like `_daemon`); `_cmd_status` prints a
  `board:` line following the `notify:` pattern.
- `src/vupai/config.py` - new `board_*` fields + a `_FIELD_BLOCKS` entry each
  (the drift-guard test `test_template_covers_every_config_field` fails
  otherwise).
- `src/vupai/tmuxio.py` - extend `split_window` with an optional size/orientation
  (or add `open_board_pane`), and a one-line `mark_board_pane(id)` wrapper.
- `AGENTS.md` / `README.md` - document the command, config, and the Haiku cost
  recommendation.

## 10. Config fields (kept minimal; tuning constants live in code)

User-facing TOML fields (each follows the `notify_*` pattern: dataclass field +
`_FIELD_BLOCKS` block):

```
board_enabled              : bool  = False        # reserved for future auto-open (v1: manual only)
board_summarizer_cmd       : str   = "claude -p --model claude-haiku-4-5"   # swappable; Haiku keeps cost low
board_poll_interval        : float = 2.0
board_min_summary_interval : float = 30.0
board_summary_timeout_s    : float = 12.0
```

The config comment for `board_summarizer_cmd` documents the swap targets
(`codex exec`, `gemini -p`, `ollama run <model>`) and notes that Haiku is chosen
for cost; a one-line glance summary does not need a high-tier model.

Internal constants (constructor args for tests, not in TOML, to avoid config
sprawl): `board_capture_lines=40`, `board_tail_bytes=6000`, `settle_ticks=2`,
`churn_active=0.10`, `churn_idle=0.01`, `board_summary_max_chars=100`,
`board_max_concurrent=2`.

`vupai board` works whether or not `board_enabled` is set; the flag only governs
auto-open on session start.

## 11. Testing

Mirrors the existing injected-collaborators, no-threads/no-tmux test style.

- `test_panestate.py`: churn classifier (frame sequences -> states, settle
  behavior, hysteresis dead-band), markers accelerate/override, a no-marker tool
  still reaches SETTLED from churn alone (the anti-lock-in test). Keep
  `test_watcher.py` green via the re-export.
- `test_summarize.py`: fake `cmd` (a `printf`/`cat` script) -> last-line
  extraction; a codex-style interleaved-stdout fixture -> still extracts the
  final line; timeout -> fallback; missing command -> fallback; `NEEDS:` parsing;
  swap `board_summarizer_cmd` to a non-Claude fake and assert the whole pipeline
  works with zero Claude involvement.
- `test_board.py`: edge fires summary once; unchanged tail -> no second call
  (hash gate); throttle; in-flight guard; self-exclusion (`$TMUX_PANE` and
  `@vupai_board`); cold-start; `render_frame` snapshot.
- `test_cli.py`: `_cmd_board` splits + marks a pane (FakeTmux asserts calls);
  parser accepts `board` and `_board`; `_cmd_status` prints the board line.
- `test_config.py`: board defaults, load round-trip, template-covers-every-field.

Run gate: `uv run python -m pytest -m "not integration and not slow"` and
`uv run ruff check`.

## 12. Build order

1. `panestate.py` extraction + churn classifier + `MARKERS`; refactor
   `watcher.py` to import; confirm `test_watcher.py` green. (`test_panestate.py`)
2. `summarize.py` + fallback. (`test_summarize.py`)
3. `board.py` engine + `render_frame`. (`test_board.py`)
4. `cli` `board`/`_board` + `tmuxio` split/mark helpers + config fields +
   `_cmd_status`. (`test_cli.py`, `test_config.py`)
5. Docs (AGENTS.md, README), manual end-to-end in a real tmux session.

## 13. Deferred (explicit cut lines for v1)

- Per-tool MARKERS tables beyond Claude (churn covers other tools already).
- Configurable board geometry / resize handling.
- A pane-border one-line summary as a second renderer (the dedicated pane is the
  target).
- Cross-session / all-panes scope (focused session only).
- Batching multiple panes into one summarizer prompt (a fork-count optimization;
  with the hash cache, steady state is usually one pane settling at a time).
- Audio/chime on board updates (the watcher owns notifications).
- A running-board config reload (reopen the pane instead).

Cut rule: any feature needing a new tmux primitive beyond
`split_window` / `select_layout` / `kill_pane` / `capture_pane` + the one
`mark_board_pane` line is deferred.

## 14. Decisions (locked)

1. **Summarizer model default:** `claude -p --model claude-haiku-4-5`. Cheap for
   a one-line glance summary; ~$0.19/hr realistic, ~$2/hr worst case (N=6).
2. **Launch:** manual only in v1 (`vupai board`). `board_enabled` is reserved for
   a future auto-open on `vupai up`.
3. **Placement:** right-side vertical split, ~40% width.
