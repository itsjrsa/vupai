from pathlib import Path

import pytest

from voxpane.asr import ParakeetTranscriber, Transcriber


# ---- A fake Transcriber other layers (daemon, etc.) can depend on ----
class FakeTranscriber:
    """Hand-rolled Transcriber for use by higher layers in their unit tests."""

    def __init__(self, text: str = "") -> None:
        self._text = text
        self.warmed = 0
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def warm(self) -> None:
        self.warmed += 1

    def transcribe(self, wav_path: Path, hints=()) -> str:
        self.calls.append((wav_path, tuple(hints)))
        return self._text


def test_fake_transcriber_satisfies_protocol() -> None:
    # Runtime structural check: FakeTranscriber is a valid Transcriber.
    ft = FakeTranscriber("hello")
    assert isinstance(ft, Transcriber)
    assert ft.transcribe(Path("/tmp/x.wav")) == "hello"


# ---- Fakes for the parakeet_mlx module surface ----
class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModel:
    def __init__(self, text: str) -> None:
        self._text = text
        self.transcribe_calls: list[str] = []
        self.hotwords_calls: list[list[str]] = []

    def transcribe(self, path: str, hotwords=None) -> _FakeResult:
        self.transcribe_calls.append(path)
        if hotwords is not None:
            self.hotwords_calls.append(list(hotwords))
        return _FakeResult(self._text)


@pytest.fixture
def patched_parakeet(monkeypatch):
    """Patch voxpane.asr.from_pretrained; record how many times it loads."""
    state = {"loads": 0, "last_model_id": None, "model": _FakeModel("  routed text \n")}

    def fake_from_pretrained(model_id: str):
        state["loads"] += 1
        state["last_model_id"] = model_id
        return state["model"]

    monkeypatch.setattr("voxpane.asr.from_pretrained", fake_from_pretrained)
    return state


def test_warm_loads_model_with_id(patched_parakeet) -> None:
    t = ParakeetTranscriber("some/model-id")
    t.warm()
    assert patched_parakeet["loads"] == 1
    assert patched_parakeet["last_model_id"] == "some/model-id"


def test_warm_is_idempotent(patched_parakeet) -> None:
    t = ParakeetTranscriber("some/model-id")
    t.warm()
    t.warm()
    t.warm()
    assert patched_parakeet["loads"] == 1  # cached on the instance


def test_transcribe_returns_stripped_text(patched_parakeet) -> None:
    t = ParakeetTranscriber("some/model-id")
    t.warm()
    out = t.transcribe(Path("/tmp/utt.wav"))
    assert out == "routed text"  # leading/trailing whitespace stripped
    assert patched_parakeet["model"].transcribe_calls == ["/tmp/utt.wav"]


def test_transcribe_auto_warms_when_cold(patched_parakeet) -> None:
    t = ParakeetTranscriber("some/model-id")
    # No explicit warm() call.
    out = t.transcribe(Path("/tmp/utt.wav"))
    assert out == "routed text"
    assert patched_parakeet["loads"] == 1  # transcribe triggered exactly one load


def test_transcribe_forwards_hints_as_hotwords(patched_parakeet) -> None:
    t = ParakeetTranscriber("some/model-id")
    out = t.transcribe(Path("/tmp/utt.wav"), hints=["alpha", "bravo"])
    assert out == "routed text"
    model = patched_parakeet["model"]
    # Path is passed as a str, and the live names are forwarded as hotwords.
    assert model.transcribe_calls == ["/tmp/utt.wav"]
    assert model.hotwords_calls == [["alpha", "bravo"]]


def test_transcribe_without_hints_omits_hotwords(patched_parakeet) -> None:
    t = ParakeetTranscriber("some/model-id")
    t.transcribe(Path("/tmp/utt.wav"))
    assert patched_parakeet["model"].hotwords_calls == []  # no biasing requested


def test_transcribe_falls_back_when_model_rejects_hotwords(monkeypatch) -> None:
    # Older parakeet builds whose transcribe() has no hotwords kwarg must still work.
    class NoHotwordsModel:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def transcribe(self, path: str) -> _FakeResult:  # no hotwords kwarg
            self.calls.append(path)
            return _FakeResult("hi")

    model = NoHotwordsModel()
    monkeypatch.setattr("voxpane.asr.from_pretrained", lambda model_id: model)
    t = ParakeetTranscriber("x")
    out = t.transcribe(Path("/tmp/a.wav"), hints=["z"])
    assert out == "hi"
    assert model.calls == ["/tmp/a.wav"]


@pytest.mark.slow
def test_real_model_smoke() -> None:
    """Skipped by default (run with `-m slow`). Loads the real model."""
    wav = Path(__file__).parent / "fixtures" / "tiny.wav"
    if not wav.exists():
        pytest.skip("tests/fixtures/tiny.wav not present")
    t = ParakeetTranscriber("mlx-community/parakeet-tdt-0.6b-v3")
    t.warm()
    out = t.transcribe(wav)
    assert isinstance(out, str)
