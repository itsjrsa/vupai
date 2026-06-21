"""Utterance journal: a structured JSONL trail of what was heard and done.

One JSON object per line is appended for each utterance, capturing the
transcript, the decision vupai reached (command/route/dictation/unknown/...)
and the outcome (injected/failed/...). The point is reviewing and diagnosing
misfires after the fact. Audio retention is opt-in: when enabled, the wav is
copied next to the journal and the directory is ring-bounded by file count, so
a past misfire can be replayed offline through a different model or parser.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

JOURNAL_PATH = Path.home() / ".config" / "vupai" / "journal.jsonl"


class Journal:
    """Append-only JSONL utterance log with optional ring-bounded audio."""

    def __init__(self, path: Path = JOURNAL_PATH, *, enabled: bool = True,
                 keep_audio: bool = False, audio_retention: int = 500) -> None:
        self._path = path
        self._enabled = enabled
        self._keep_audio = keep_audio
        self._audio_dir = path.parent / "audio"
        self._audio_retention = max(0, audio_retention)

    @classmethod
    def from_config(cls, config, path: Path = JOURNAL_PATH) -> "Journal":
        return cls(
            path,
            enabled=config.journal_enabled,
            keep_audio=config.journal_keep_audio,
            audio_retention=config.journal_audio_retention,
        )

    def record(self, entry: dict, wav: Path | None = None) -> None:
        """Append one entry. A no-op when disabled. Best-effort: a journaling
        failure must never disturb the live pipeline, so all IO is guarded."""
        if not self._enabled:
            return
        entry = dict(entry)
        if self._keep_audio and wav is not None:
            try:
                stored = self._store_audio(wav)
                entry["wav"] = stored.name
            except OSError:
                logger.warning("journal: failed to retain audio for %s", wav)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("journal: failed to append entry")

    def _store_audio(self, wav: Path) -> Path:
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        dest = self._audio_dir / wav.name
        shutil.copyfile(wav, dest)
        self._prune_audio()
        return dest

    def _prune_audio(self) -> None:
        """Keep only the newest `audio_retention` wavs; delete the rest."""
        wavs = sorted(
            self._audio_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        excess = len(wavs) - self._audio_retention
        for stale in wavs[:max(0, excess)]:
            stale.unlink(missing_ok=True)
