# Changelog

All notable changes to vupai are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor
bumps may carry breaking changes).

Every pull request and release should add its user-facing changes under
`## [Unreleased]`. Cutting a release renames that section to the new version with
a date, bumps `version` in `pyproject.toml`, and tags `vX.Y.Z`.

## [Unreleased]

### Added

- Cross-pane activity ledger (Layer 1): a pull-only background poller records
  which sibling pane touched which file per git tree into
  `.vupai/activity.current.json` (+ ring-bounded `activity.jsonl` history).
  Read it with `vupai activity`, the `activity` voice verb, or directly from
  the JSON. New `activity_*` config keys. `vupai activity --stats` reports the
  Phase 0 contention/attribution counters.

## [0.3.0] - 2026-06-26

### Added

- Spoken feedback now names the program when creating a named agent: "open one
  codex" says "opening a codex agent" (and "N codex agents up") instead of the
  bare "agent". Config-default and explicit-shell creates keep generic wording.
- CI workflow (unit tests + lint on macOS, secret scan) on every push and PR.
- Repository governance: `CONTRIBUTING.md` (not accepting external contributions
  for now) and `SECURITY.md` (private vulnerability reporting).

### Fixed

- Named agents are now actually creatable by voice. Parakeet never transcribes
  "claude" literally (it lands as "cloth"/"cloud"), so "open one claude" silently
  did nothing; the same gap hit "pi" (heard as "pie"/"py") and "codex" when split
  into "code x". These mishearings now resolve to the right program.
- "two" mis-heard as "to" ("open to shell" == "open two shell") now parses as a
  count instead of falling through to dictation; "ten" recovers from "tent". The
  count aliases are scoped to the create parse, so "to" the preposition is
  unaffected elsewhere.
- "start" is now a create verb ("start one agent" == "open one agent").
- Explicit-shell creates voice "opening a shell" instead of "opening an agent".

## [0.2.0] - 2026-06-26

### Removed

- Legacy single-key `keyword` addressing mode and the `addressing` config field.
  vupai is now two-key only: the dictation key (`hotkey`) types verbatim into the
  focused pane and the system key (`command_hotkey`) runs the command layer. An
  `addressing = ...` line left in `config.toml` is ignored (with a warning), and
  misconfigured keys now fall back to the defaults (`alt_r` / `cmd_r`) instead of a
  single-key listener.

### Added

- `gitleaks` pre-commit hook that blocks commits containing secrets (keys, tokens,
  credentials). Enable it once per clone with `uv run pre-commit install`; scan the
  whole tree on demand with `uv run pre-commit run --all-files`.

### Fixed

- `read` no longer speaks an agent's splash/status chrome as part of its spoken
  pane summary.

## [0.1.0] - 2026-06-21

- Initial public release: push-to-talk voice control over tmux agent panes
  (record → transcribe → route → inject), the command layer, supervision board,
  spoken read-back, and the `vupai` CLI.

[Unreleased]: https://github.com/itsjrsa/vupai/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/itsjrsa/vupai/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/itsjrsa/vupai/releases/tag/v0.1.0
