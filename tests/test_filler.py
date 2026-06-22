from vupai.filler import strip_fillers

WORDS = frozenset({"um", "uh", "er", "ah", "eh", "hmm", "mm"})


def test_removes_standalone_filler():
    assert strip_fillers("um hello there", WORDS) == "Hello there"


def test_removes_midsentence_filler():
    assert strip_fillers("open the uh door", WORDS) == "open the door"


def test_substring_is_safe():
    assert strip_fillers("an umbrella and hummus", WORDS) == "an umbrella and hummus"


def test_repeated_letter_variants():
    assert strip_fillers("ummm okay uhh fine", WORDS) == "Okay fine"


def test_capitalization_fixed_when_leading_removed():
    assert strip_fillers("Um, send it", WORDS) == "Send it"


def test_orphaned_leading_punctuation_cleaned():
    assert strip_fillers("uh, hello", WORDS) == "Hello"


def test_all_filler_collapses_to_empty():
    assert strip_fillers("um uh hmm", WORDS) == ""


def test_empty_input():
    assert strip_fillers("", WORDS) == ""


def test_custom_word_set_only():
    assert strip_fillers("um yeah", frozenset({"yeah"})) == "um"


def test_case_insensitive():
    assert strip_fillers("UH okay", WORDS) == "Okay"


def test_midsentence_removal_preserves_lowercase():
    # No leading filler removed -> first word casing untouched.
    assert strip_fillers("hello uh world", WORDS) == "hello world"


def test_leading_punctuation_before_filler_recapitalizes():
    assert strip_fillers("...um hello", WORDS) == "Hello"
