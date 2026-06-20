"""Speech-to-text via parakeet-mlx, behind a fakeable Protocol."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

# Imported at module top so tests can monkeypatch `voxpane.asr.from_pretrained`.
from parakeet_mlx import from_pretrained

logger = logging.getLogger(__name__)


@runtime_checkable
class Transcriber(Protocol):
    def warm(self) -> None: ...
    def transcribe(self, wav_path: Path, hints: Sequence[str] = ()) -> str: ...


class ParakeetTranscriber:
    """Transcriber backed by a parakeet-mlx model, lazily loaded and cached."""

    def __init__(self, model_id: str) -> None:
        self._model_id = model_id
        self._model = None  # populated by warm(); cached for the process lifetime
        # Tri-state hotword-support probe: None = unknown, True/False = known after
        # the first hinted transcribe. Avoids re-raising TypeError every utterance.
        self._supports_hotwords: bool | None = None

    def warm(self) -> None:
        """Load and cache the model. Idempotent: a second call is a no-op."""
        if self._model is not None:
            return
        logger.info("loading parakeet model %s", self._model_id)
        # Surface a misconfigured/stale multilingual model: v3 does per-utterance
        # language detection and drifts to German/Russian on short audio. v2 is
        # English-only. Warn loudly so a wrong model_id is visible in the log.
        lowered = self._model_id.lower()
        if "multilingual" in lowered or "v3" in lowered:
            logger.warning(
                "model %s looks multilingual - it may drift to non-English "
                "transcriptions on short audio; prefer the English-only v2 model",
                self._model_id)
        self._model = from_pretrained(self._model_id)

    def transcribe(self, wav_path: Path, hints: Sequence[str] = ()) -> str:
        """Transcribe a wav file, auto-warming if the model is cold.

        `hints` (the live agent names) are forwarded as decode hotwords to bias
        recognition toward them. Hotword support is best-effort: if the installed
        parakeet-mlx build's transcribe() has no `hotwords` kwarg, we fall back to
        a plain transcribe so behaviour degrades gracefully.
        """
        if self._model is None:
            self.warm()
        path = str(wav_path)
        if hints and self._supports_hotwords is not False:
            try:
                result = self._model.transcribe(path, hotwords=list(hints))
                self._supports_hotwords = True
            except TypeError:
                # Probe failed once: cache it so we never re-attempt (and re-log).
                if self._supports_hotwords is None:
                    logger.info(
                        "parakeet build has no hotwords kwarg; name biasing "
                        "disabled (router name-matching is unaffected)")
                self._supports_hotwords = False
                result = self._model.transcribe(path)
        else:
            result = self._model.transcribe(path)
        text = getattr(result, "text", "")
        return str(text).strip()
