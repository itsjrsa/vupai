from __future__ import annotations

import json
import wave
from pathlib import Path

from voxpane.journal import Journal


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _make_wav(path: Path, frames: int = 1600) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * frames)
    return path


def test_record_appends_jsonl_line(tmp_path):
    j = Journal(tmp_path / "journal.jsonl")
    j.record({"transcript": "create a new pane", "decision": "create"})
    j.record({"transcript": "nova run tests", "decision": "route"})

    lines = _read_lines(tmp_path / "journal.jsonl")
    assert [e["transcript"] for e in lines] == [
        "create a new pane", "nova run tests"]


def test_disabled_writes_nothing(tmp_path):
    path = tmp_path / "journal.jsonl"
    j = Journal(path, enabled=False)
    j.record({"transcript": "hello"})
    assert not path.exists()


def test_transcripts_only_does_not_retain_audio(tmp_path):
    wav = _make_wav(tmp_path / "utt.wav")
    j = Journal(tmp_path / "journal.jsonl", keep_audio=False)
    j.record({"transcript": "hi"}, wav=wav)

    entry = _read_lines(tmp_path / "journal.jsonl")[0]
    assert "wav" not in entry
    assert not (tmp_path / "audio").exists()


def test_keep_audio_copies_wav_and_links_it(tmp_path):
    wav = _make_wav(tmp_path / "utt.wav")
    j = Journal(tmp_path / "journal.jsonl", keep_audio=True)
    j.record({"transcript": "hi"}, wav=wav)

    entry = _read_lines(tmp_path / "journal.jsonl")[0]
    assert entry["wav"] == "utt.wav"
    stored = tmp_path / "audio" / "utt.wav"
    assert stored.exists()
    # Original is copied, not moved.
    assert wav.exists()


def test_audio_ring_prunes_oldest(tmp_path):
    j = Journal(tmp_path / "journal.jsonl", keep_audio=True, audio_retention=2)
    for i in range(4):
        wav = _make_wav(tmp_path / f"u{i}.wav")
        # Stagger mtimes so prune order is deterministic.
        import os
        os.utime(wav, (i, i))
        j.record({"transcript": str(i)}, wav=wav)
        os.utime(tmp_path / "audio" / f"u{i}.wav", (i, i))

    kept = sorted(p.name for p in (tmp_path / "audio").glob("*.wav"))
    assert kept == ["u2.wav", "u3.wav"]
    # All four utterances are still journaled; only audio is bounded.
    assert len(_read_lines(tmp_path / "journal.jsonl")) == 4


def test_from_config_reads_knobs(tmp_path):
    from voxpane.config import Config

    cfg = Config(journal_enabled=True, journal_keep_audio=True,
                 journal_audio_retention=7)
    j = Journal.from_config(cfg, tmp_path / "journal.jsonl")
    assert j._enabled is True
    assert j._keep_audio is True
    assert j._audio_retention == 7
