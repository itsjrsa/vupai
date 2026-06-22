import pytest

from vupai.registry import Pane
from vupai.router import (
    CALLSIGNS,
    name_collides,
    next_callsign,
    resolve_pane_by_name,
    route,
    word_to_int,
)


def test_word_to_int_digits_and_words():
    assert word_to_int("4") == 4
    assert word_to_int("four") == 4
    assert word_to_int("nine") == 9
    assert word_to_int("zero") is None
    assert word_to_int("frontend") is None
    assert word_to_int("12") == 12  # raw digits not capped here
    assert word_to_int("ten") == 10
    assert word_to_int("twelve") == 12
    assert word_to_int("twenty") == 20
    assert word_to_int("thirty") == 30


def mk(id: str, window_id: str, window: str, index: int, name: str,
       command: str = "claude", active: bool = False) -> Pane:
    return Pane(id=id, window_id=window_id, window=window, index=index,
                name=name, command=command, active=active)


# A two-window layout. Window @1 "main" has panes %1 (frontend) and %2 (backend);
# window @2 "side" has pane %3 (docs).
@pytest.fixture
def panes() -> list[Pane]:
    return [
        mk("%1", "@1", "main", 1, "frontend", active=True),
        mk("%2", "@1", "main", 2, "backend"),
        mk("%3", "@2", "side", 1, "docs"),
    ]


def test_resolve_exact(panes):
    m = resolve_pane_by_name("backend", panes, fuzzy_cutoff=82)
    assert (m.pane_id, m.matched_name, m.confidence, m.candidates) == ("%2", "backend", 100.0, ())


def test_resolve_fuzzy(panes):
    m = resolve_pane_by_name("frontnd", panes, fuzzy_cutoff=82)
    assert m.pane_id == "%1" and 82 <= m.confidence < 100 and m.candidates == ()


def test_resolve_no_match(panes):
    m = resolve_pane_by_name("zzzz", panes, fuzzy_cutoff=82)
    assert m.pane_id is None and m.candidates == ()


def test_resolve_ambiguous():
    p = [mk("%1", "@1", "main", 1, "nova"), mk("%2", "@1", "main", 2, "novo")]
    m = resolve_pane_by_name("nov", p, fuzzy_cutoff=82)
    assert m.pane_id is None and set(m.candidates) == {"nova", "novo"}


def test_exact_match_case_insensitive(panes):
    r = route("Frontend add a dark mode toggle", panes, focused_id="%3")
    assert r.pane_id == "%1"
    assert r.matched_name == "frontend"
    assert r.confidence == 100
    assert r.fallback is False
    # Leading name token stripped, remainder casing preserved.
    assert r.text == "add a dark mode toggle"


def test_exact_match_strips_trailing_punctuation_on_token(panes):
    # ASR often emits "backend," with a comma; token compare strips punctuation.
    r = route("backend, run the tests", panes, focused_id="%1")
    assert r.pane_id == "%2"
    assert r.matched_name == "backend"
    assert r.confidence == 100
    assert r.fallback is False
    assert r.text == "run the tests"


def test_fuzzy_match_for_mangled_name(panes):
    # ASR mangles "backend" -> "back end" collapses to token "back"? Use a clear
    # single-token mangling that exact fails but rapidfuzz catches.
    r = route("frontnd add a button", panes, focused_id="%3")
    assert r.pane_id == "%1"
    assert r.matched_name == "frontend"
    assert 82 <= r.confidence < 100
    assert r.fallback is False
    assert r.text == "add a button"


def test_phonetic_only_match_via_metaphone(panes):
    # "phrunt end" style spelling: low edit-distance fails fuzzy but the
    # double-metaphone primary code matches. We assert confidence 70 + match.
    p = [mk("%9", "@1", "main", 1, "kris")]  # kris ~ metaphone of "chris"
    r = route("chris what is the status", p, focused_id=None)
    assert r.pane_id == "%9"
    assert r.matched_name == "kris"
    assert r.confidence == 70
    assert r.fallback is False
    assert r.text == "what is the status"


def test_no_match_focus_fallback_keeps_text(panes):
    r = route("please deploy now", panes, focused_id="%2")
    assert r.pane_id == "%2"
    assert r.matched_name is None
    assert r.confidence == 0
    assert r.fallback is True
    # Text unchanged on fallback.
    assert r.text == "please deploy now"


def test_number_word_routes_within_focused_window(panes):
    # Focused pane is %1 (window @1). "two" -> pane_index 2 in @1 == %2.
    r = route("two run the migration", panes, focused_id="%1")
    assert r.pane_id == "%2"
    assert r.matched_name is None
    assert r.confidence == 100
    assert r.fallback is False
    assert r.text == "run the migration"


def test_number_digit_routes_within_focused_window(panes):
    r = route("2 stop the server", panes, focused_id="%1")
    assert r.pane_id == "%2"
    assert r.confidence == 100
    assert r.text == "stop the server"
    assert r.fallback is False


def test_number_routes_positionally_with_base0_indices():
    # tmux's default pane-base-index is 0: a stock window has panes 0,1,2...
    # Spoken numbers are 1-based, so "one" must hit the FIRST pane (index 0),
    # "two" the SECOND (index 1). Routing is positional, not index==n.
    p = [mk("%1", "@1", "main", 0, "frontend", active=True),
         mk("%2", "@1", "main", 1, "backend")]
    r1 = route("one run it", p, focused_id="%1")
    assert r1.pane_id == "%1" and r1.match_method == "number" and r1.text == "run it"
    r2 = route("two run it", p, focused_id="%1")
    assert r2.pane_id == "%2" and r2.match_method == "number" and r2.text == "run it"


def test_number_beyond_pane_count_falls_back():
    # "three" in a two-pane window has no target -> verbatim focus fallback.
    p = [mk("%1", "@1", "main", 0, "frontend", active=True),
         mk("%2", "@1", "main", 1, "backend")]
    r = route("three do the thing", p, focused_id="%1")
    assert r.fallback is True
    assert r.text == "three do the thing"


def test_number_with_no_focus_is_not_a_match(panes):
    # No focused window to resolve the index against -> fall through to fallback.
    r = route("two run the migration", panes, focused_id=None)
    assert r.pane_id is None
    assert r.fallback is True
    assert r.text == "two run the migration"


def test_no_focus_and_no_name_match_yields_none(panes):
    r = route("just do the thing", panes, focused_id=None)
    assert r.pane_id is None
    assert r.matched_name is None
    assert r.confidence == 0
    assert r.fallback is True
    assert r.text == "just do the thing"


def test_empty_transcript_is_focus_fallback(panes):
    r = route("   ", panes, focused_id="%3")
    assert r.pane_id == "%3"
    assert r.fallback is True
    assert r.text == "   "


def test_name_collides_detects_confusable(panes):
    # "frontnd" is fuzzily confusable with existing "frontend".
    assert name_collides("frontnd", ["frontend", "backend"]) == "frontend"


def test_name_collides_allows_distinct(panes):
    assert name_collides("database", ["frontend", "backend"]) is None


def test_name_collides_detects_phonetic_only():
    # "kris" vs "chris": rapidfuzz.fuzz.ratio ~66.7 (below 82 cutoff) but
    # both share doublemetaphone primary code "KRS", so collision is phonetic only.
    assert name_collides("kris", ["chris"]) == "chris"


# ---------------------------------------------------------------------------
# Fix 1: unnamed panes (name == id) must not be matched by name tiers
# ---------------------------------------------------------------------------

def test_unnamed_pane_not_matched_by_name_tiers():
    # Pane %2 has name == id (tmux pseudo-title): name routing must skip it.
    unnamed = mk("%2", "@1", "main", 2, "%2")
    named = mk("%1", "@1", "main", 1, "frontend")
    panes = [named, unnamed]

    # Exact: token "%2" should NOT route to the unnamed pane via name matching.
    r = route("%2 do the thing", panes, focused_id="%1")
    # Should fall through to number or fallback, not match by name.
    # "%2" is not a digit/number-word so it falls back to focus.
    assert r.fallback is True
    assert r.pane_id == "%1"

    # Named pane is still routable by its real name.
    r2 = route("frontend run tests", panes, focused_id="%2")
    assert r2.pane_id == "%1"
    assert r2.matched_name == "frontend"


def test_number_routing_still_works_with_unnamed_panes():
    # Number routing (pane_index) must still consider all panes,
    # including unnamed ones.
    unnamed = mk("%2", "@1", "main", 2, "%2")
    named = mk("%1", "@1", "main", 1, "frontend", active=True)
    panes = [named, unnamed]

    r = route("two run the tests", panes, focused_id="%1")
    assert r.pane_id == "%2"   # number routing reached the unnamed pane
    assert r.fallback is False
    assert r.confidence == 100


def test_name_collides_skips_pseudo_titles():
    # A pseudo-title like "%3" should not be treated as a colliding real name.
    assert name_collides("alpha", ["%1", "%3", "beta"]) is None
    # Real names are still checked.
    assert name_collides("alpha", ["alpha", "%2"]) == "alpha"


# ---------------------------------------------------------------------------
# Auto-assigned callsigns for new panes
# ---------------------------------------------------------------------------

def test_next_callsign_picks_first_when_none_used():
    assert next_callsign([]) == CALLSIGNS[0]


def test_next_callsign_skips_used_and_confusable():
    # CALLSIGNS[0] taken outright; CALLSIGNS[1] blocked by a fuzzy near-match.
    used = [CALLSIGNS[0], CALLSIGNS[1] + "x"]
    pick = next_callsign(used)
    assert pick == CALLSIGNS[2]


def test_next_callsign_ignores_unnamed_pseudo_titles():
    # Pseudo-titles (%N) are not real names; the first callsign stays available.
    assert next_callsign(["%1", "%2"]) == CALLSIGNS[0]


def test_next_callsign_returns_none_when_pool_exhausted():
    assert next_callsign(list(CALLSIGNS)) is None


def test_callsign_pool_yields_30_distinct_from_empty():
    # A max-count create (commands.MAX_CREATE_COUNT == 30) from a fresh window
    # must be able to name every pane. Simulate _exec_create's assignment loop:
    # any two confusable entries collapse (the second is skipped), so this guards
    # that the curated list has >= 30 mutually non-confusable callsigns.
    from vupai.commands import MAX_CREATE_COUNT

    used: list[str] = []
    for _ in range(MAX_CREATE_COUNT):
        name = next_callsign(used)
        assert name is not None, f"pool exhausted after {len(used)} of {MAX_CREATE_COUNT}"
        used.append(name)
    assert len(set(used)) == MAX_CREATE_COUNT


# ---------------------------------------------------------------------------
# #2: ambiguous near-tie name match surfaces candidates instead of guessing
# ---------------------------------------------------------------------------

def test_fuzzy_near_tie_is_ambiguous():
    # token "nov" scores ~85.7 against BOTH "nova" and "novo" (within margin):
    # too close to call -> ambiguous, route nowhere, surface both candidates.
    p = [mk("%1", "@1", "main", 1, "nova"), mk("%2", "@1", "main", 2, "novo")]
    r = route("nov run the tests", p, focused_id="%1")
    assert r.pane_id is None
    assert r.fallback is False
    assert set(r.candidates) == {"nova", "novo"}
    # transcript left intact so the user can re-say a clearer name
    assert r.text == "nov run the tests"


def test_fuzzy_clear_winner_is_not_ambiguous():
    # Only "nova" clears the cutoff ("zebra" scores 0) -> single unambiguous match.
    p = [mk("%1", "@1", "main", 1, "nova"), mk("%2", "@1", "main", 2, "zebra")]
    r = route("nov ship it", p, focused_id="%2")
    assert r.pane_id == "%1"
    assert r.matched_name == "nova"
    assert r.candidates == ()
    assert r.text == "ship it"


def test_exact_match_wins_over_fuzzy_near_tie():
    # An exact name match short-circuits before ambiguity detection.
    p = [mk("%1", "@1", "main", 1, "nova"), mk("%2", "@1", "main", 2, "novo")]
    r = route("nova deploy", p, focused_id="%2")
    assert r.pane_id == "%1"
    assert r.matched_name == "nova"
    assert r.candidates == ()


# ---------------------------------------------------------------------------
# Vocative filler peel: "okay Atlas, run the tests" -> address Atlas.
# Exact-anchor: a filler is dropped ONLY when an EXACT name follows; otherwise
# the original transcript is injected verbatim (non-destructive).
# ---------------------------------------------------------------------------

def test_filler_then_exact_name_routes():
    p = [mk("%1", "@1", "main", 1, "atlas"), mk("%2", "@1", "main", 2, "nova")]
    r = route("okay atlas run the tests", p, focused_id="%2")
    assert r.pane_id == "%1"
    assert r.matched_name == "atlas"
    assert r.text == "run the tests"
    assert r.fallback is False


def test_filler_with_punctuation_routes():
    p = [mk("%1", "@1", "main", 1, "nova")]
    r = route("hey, nova ship it", p, focused_id="%1")
    assert r.pane_id == "%1" and r.text == "ship it"


def test_two_fillers_then_name_routes():
    p = [mk("%1", "@1", "main", 1, "nova"), mk("%2", "@1", "main", 2, "atlas")]
    r = route("um okay nova status", p, focused_id="%2")
    assert r.pane_id == "%1" and r.text == "status"


def test_filler_alone_is_verbatim_focus():
    # No name after the filler -> peel discarded, original injected to focus.
    p = [mk("%1", "@1", "main", 1, "nova")]
    r = route("okay let us refactor this", p, focused_id="%1")
    assert r.fallback is True
    assert r.pane_id == "%1"
    assert r.text == "okay let us refactor this"   # UNCHANGED


def test_filler_then_fuzzy_name_does_not_route():
    # Exact-anchor: a near-miss after a filler is NOT matched (no fuzzy/phonetic
    # on the peeled token). "member" must not become "ember".
    p = [mk("%1", "@1", "main", 1, "ember")]
    r = route("okay member should review", p, focused_id="%1")
    assert r.fallback is True
    assert r.text == "okay member should review"


def test_filler_then_number_does_not_route():
    # Exact-anchor excludes number routing on the peeled token: "okay two ..."
    # in a tiled window stays verbatim dictation, not a route to pane 2.
    p = [mk("%1", "@1", "main", 1, "nova"), mk("%2", "@1", "main", 2, "atlas")]
    r = route("okay two more things", p, focused_id="%1")
    assert r.fallback is True
    assert r.text == "okay two more things"


def test_pane_named_like_filler_still_reachable():
    # A pane literally named "hey" is matched by the RAW pass before any filler
    # logic runs, so it stays addressable.
    p = [mk("%1", "@1", "main", 1, "hey"), mk("%2", "@1", "main", 2, "nova")]
    r = route("hey run the build", p, focused_id="%2")
    assert r.pane_id == "%1" and r.text == "run the build"


def test_no_filler_unmatched_is_unchanged():
    # Regression: a non-filler leading word still falls back verbatim.
    p = [mk("%1", "@1", "main", 1, "nova")]
    r = route("so deploy the staging build", p, focused_id="%1")
    assert r.fallback is True
    assert r.text == "so deploy the staging build"


# ---------------------------------------------------------------------------
# Task 1: match_method exposes the cascade stage that matched
# ---------------------------------------------------------------------------


def _pane(pid, name, *, index=0, window_id="@1", focused=False):
    return Pane(id=pid, window_id=window_id, window="main", index=index,
                name=name, command="node", active=focused)


def test_route_match_method_exact():
    panes = [_pane("%1", "nova", focused=True)]
    r = route("nova run it", panes, "%1")
    assert r.matched_name == "nova"
    assert r.match_method == "exact"


def test_route_match_method_fuzzy():
    # "novva" is close enough to fuzzy-match "nova" (>= default cutoff 82).
    panes = [_pane("%1", "nova", focused=True)]
    r = route("novva run it", panes, "%1")
    assert r.matched_name == "nova"
    assert r.match_method == "fuzzy"


def test_route_match_method_number():
    panes = [_pane("%1", "nova", index=1, focused=True),
             _pane("%2", "ember", index=2)]
    r = route("two run it", panes, "%1")
    assert r.pane_id == "%2"
    assert r.match_method == "number"


def test_route_match_method_focus_fallback():
    panes = [_pane("%1", "nova", focused=True)]
    r = route("just dictate this", panes, "%1")
    assert r.fallback is True
    assert r.match_method == "focus_fallback"


def test_resolve_pane_by_name_reports_method():
    panes = [_pane("%1", "nova")]
    assert resolve_pane_by_name("nova", panes).method == "exact"
    assert resolve_pane_by_name("novva", panes).method == "fuzzy"
    assert resolve_pane_by_name("zzzznope", panes).method is None
