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

- CI workflow (unit tests + lint on macOS, secret scan) on every push and PR.
- Repository governance: `CONTRIBUTING.md` (not accepting external contributions
  for now) and `SECURITY.md` (private vulnerability reporting).

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
