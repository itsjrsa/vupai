import subprocess

from vupai.speech import speak


class _FakeProc:
    def __init__(self, argv):
        self.argv = argv


def _spawn(captured):
    """Fake subprocess.Popen: records (argv, kwargs), returns a stand-in handle."""
    def spawn(argv, **kwargs):
        captured.append((argv, kwargs))
        return _FakeProc(argv)
    return spawn


def test_speak_passes_text_as_final_argv_arg():
    cap = []
    handle = speak("nova: tests green", cmd="say", spawn=_spawn(cap))
    assert cap[0][0] == ["say", "nova: tests green"]
    assert handle is not None  # the process handle is returned (room for barge-in)


def test_speak_splits_cmd_words_and_appends_text():
    # tts_cmd may carry flags ("say -v Daniel"); the phrase is always the last arg.
    cap = []
    speak("hi", cmd="say -v Daniel", spawn=_spawn(cap))
    assert cap[0][0] == ["say", "-v", "Daniel", "hi"]


def test_speak_strips_surrounding_whitespace():
    cap = []
    speak("  hi there \n", cmd="say", spawn=_spawn(cap))
    assert cap[0][0] == ["say", "hi there"]


def test_speak_blank_text_is_noop():
    cap = []
    assert speak("   ", cmd="say", spawn=_spawn(cap)) is None
    assert cap == []


def test_speak_empty_cmd_is_noop():
    cap = []
    assert speak("hi", cmd="", spawn=_spawn(cap)) is None
    assert cap == []


def test_speak_swallows_missing_command():
    def boom(argv, **kwargs):
        raise FileNotFoundError(argv[0])
    assert speak("hi", cmd="nope", spawn=boom) is None


def test_speak_swallows_generic_spawn_error():
    def boom(argv, **kwargs):
        raise OSError("device busy")
    assert speak("hi", cmd="say", spawn=boom) is None


def test_speak_is_fire_and_forget_to_devnull():
    # Non-blocking by contract (Popen, never awaited) and silent: a chatty TTS CLI
    # must not pollute the daemon log, so stdout/stderr route to DEVNULL.
    cap = []
    speak("hi", cmd="say", spawn=_spawn(cap))
    _, kwargs = cap[0]
    assert kwargs.get("stdout") == subprocess.DEVNULL
    assert kwargs.get("stderr") == subprocess.DEVNULL
