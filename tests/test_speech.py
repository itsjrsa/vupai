import subprocess
import threading

from vupai.speech import SentenceSpeaker, speak, split_sentences


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


# --- split_sentences --------------------------------------------------------

def test_split_sentences_splits_on_terminator_then_space():
    sents, rem = split_sentences("First one. Second one! Third?")
    assert sents == ["First one.", "Second one!"]
    assert rem == " Third?"  # no trailing space yet, held for the next chunk


def test_split_sentences_holds_terminator_at_buffer_end():
    # A period with nothing after it might be mid-token ("session.py"); wait.
    sents, rem = split_sentences("Working on session")
    assert sents == []
    assert rem == "Working on session"


def test_split_sentences_does_not_split_decimals_or_paths():
    sents, rem = split_sentences("Bumped to 2.0 in session.py and ran it. Done now")
    assert sents == ["Bumped to 2.0 in session.py and ran it."]
    assert rem == " Done now"


def test_split_sentences_breaks_on_newline():
    sents, rem = split_sentences("line one\nline two\n")
    assert sents == ["line one", "line two"]
    assert rem == ""


# --- SentenceSpeaker --------------------------------------------------------

class _FakeHandle:
    def __init__(self, order, text):
        self.order, self.text, self.waited = order, text, False

    def wait(self):
        self.waited = True
        self.order.append(self.text)


def _capturing_speak_one(spoken, *, order=None):
    """speak_one stub: records the phrase, returns a waitable handle (or None)."""
    def speak_one(text):
        spoken.append(text)
        return _FakeHandle(order, text) if order is not None else None
    return speak_one


def test_sentence_speaker_speaks_complete_sentences_in_order():
    spoken = []
    sp = SentenceSpeaker(_capturing_speak_one(spoken))
    sp.feed("All tests pass. The build is ")  # one complete sentence so far
    sp.feed("green now. ")                     # completes the second
    sp.close()
    assert spoken == ["All tests pass.", "The build is green now."]


def test_sentence_speaker_flushes_trailing_fragment_on_close():
    spoken = []
    sp = SentenceSpeaker(_capturing_speak_one(spoken))
    sp.feed("waiting on your review")  # no terminator
    assert spoken == []                # nothing spoken yet
    sp.close()
    assert spoken == ["waiting on your review"]  # flushed at close


def test_sentence_speaker_waits_on_each_handle_so_audio_never_overlaps():
    order = []  # records .wait() calls; proves serialization
    spoken = []
    sp = SentenceSpeaker(_capturing_speak_one(spoken, order=order))
    sp.feed("One. Two. Three. ")
    sp.close()
    assert spoken == ["One.", "Two.", "Three."]
    assert order == ["One.", "Two.", "Three."]  # each waited before the next


def test_sentence_speaker_muted_speak_one_returns_none_is_safe():
    # speak_one returning None (muted) must not raise and must consume cleanly.
    sp = SentenceSpeaker(lambda _text: None)
    sp.feed("anything at all. ")
    sp.close()  # no error


def test_sentence_speaker_swallows_speak_one_errors():
    def boom(_text):
        raise RuntimeError("tts exploded")
    sp = SentenceSpeaker(boom)
    sp.feed("boom now. ")
    sp.close()  # best-effort: the error is swallowed


def test_sentence_speaker_caps_at_max_sentences():
    spoken = []
    sp = SentenceSpeaker(_capturing_speak_one(spoken), max_sentences=2)
    sp.feed("One. Two. Three. Four. ")
    sp.close()
    assert spoken == ["One.", "Two."]  # third and fourth dropped by the cap


def test_sentence_speaker_cap_drops_trailing_fragment_on_close():
    spoken = []
    sp = SentenceSpeaker(_capturing_speak_one(spoken), max_sentences=1)
    sp.feed("Only one. and then a fragment")
    sp.close()
    assert spoken == ["Only one."]  # fragment past the cap is not flushed


def test_sentence_speaker_cancelled_feed_is_a_noop():
    spoken = []
    ev = threading.Event()
    ev.set()
    sp = SentenceSpeaker(_capturing_speak_one(spoken), cancel=ev)
    sp.feed("Should not speak. ")
    sp.close()
    assert spoken == []  # cancelled before any feed: nothing spoken


def test_sentence_speaker_cancel_midstream_stops_following_sentences():
    spoken = []
    ev = threading.Event()
    sp = SentenceSpeaker(_capturing_speak_one(spoken), cancel=ev)
    sp.feed("First. ")   # may or may not have played yet
    ev.set()             # interrupt
    sp.feed("Second. ")  # cancelled: must not enqueue
    sp.close()
    assert "Second." not in spoken
