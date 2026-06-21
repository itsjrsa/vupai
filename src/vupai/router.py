from __future__ import annotations

from dataclasses import dataclass

from metaphone import doublemetaphone
from rapidfuzz import fuzz

from vupai.registry import Pane

# Spoken number words for 1..9 -> pane index within the focused window.
_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}


def word_to_int(token: str) -> int | None:
    """Parse a spoken number token: digits ("4") or words ("four") -> int.

    No range cap here; callers apply their own bounds. Returns None when the
    token is not a number.
    """
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)

# Characters stripped from the leading token before comparison (ASR punctuation).
_STRIP = ".,!?;:'\"()[]{}"

# Leading vocative / transition fillers peeled before addressing ("okay Atlas ...",
# "hey Nova ..."). Curated to be disjoint from CALLSIGNS, the broadcast word, the
# command verbs, the number words, and the slash/unit nouns; polysemous content
# words (so/now/right/well/look/listen/all) are deliberately EXCLUDED so plain
# dictation is never corrupted. A peel is kept only when an EXACT address follows
# (see `route`), so a filler that happens to start a dictated sentence is safe.
# Shared with commands.py (the button-mode verb peel). One-line edit + a test to
# extend, like commands._UNIT_ALIASES.
_FILLERS: frozenset[str] = frozenset({
    "okay", "ok", "hey", "alright", "um", "uh", "yeah", "hi", "hello", "yo",
})
# Cap the peel so a long stutter run can never swallow real leading content.
_MAX_FILLER_PEEL = 2


def _peel_fillers(transcript: str) -> tuple[str, int]:
    """Peel up to `_MAX_FILLER_PEEL` leading filler tokens.

    Returns ``(remainder, count)`` where `remainder` is the transcript past the
    peeled fillers (original casing preserved) and `count` is how many were
    removed. Stops at the first non-filler token even if more fillers follow.
    """
    rest = transcript
    count = 0
    while count < _MAX_FILLER_PEEL:
        token, remainder = _first_token(rest)
        if token and token in _FILLERS:
            rest = remainder
            count += 1
        else:
            break
    return rest, count


@dataclass(frozen=True)
class Route:
    pane_id: str | None   # None if no focus and no match, or on an ambiguous near-tie
    text: str             # transcript with any matched leading name token stripped
    matched_name: str | None
    confidence: float     # 0..100; 100 exact, rapidfuzz score for fuzzy, 0 for focus fallback
    fallback: bool        # True when routed to focused pane (no confident name match)
    candidates: tuple[str, ...] = ()  # non-empty only on an ambiguous near-tie name match
    match_method: str | None = None  # exact|fuzzy|metaphone|number|focus_fallback


@dataclass(frozen=True)
class NameMatch:
    pane_id: str | None
    matched_name: str | None
    confidence: float
    candidates: tuple[str, ...] = ()
    method: str | None = None  # exact|fuzzy|metaphone; None on miss/ambiguity


def _first_token(transcript: str) -> tuple[str, str]:
    """Return (normalized_first_token, remainder_with_original_casing).

    The token is lowercased and stripped of surrounding punctuation; the
    remainder preserves its original casing and is left-stripped of whitespace.
    """
    stripped = transcript.lstrip()
    if not stripped:
        return "", transcript
    parts = stripped.split(None, 1)
    raw = parts[0]
    remainder = parts[1] if len(parts) == 2 else ""
    token = raw.strip(_STRIP).lower()
    return token, remainder


def _named(panes: list[Pane]) -> list[Pane]:
    """Return panes that have a real name (i.e. name != pane id)."""
    return [p for p in panes if p.name != p.id]


def _exact(token: str, panes: list[Pane]) -> Pane | None:
    for p in _named(panes):
        if p.name.lower() == token:
            return p
    return None


def _fuzzy_match(
    token: str, panes: list[Pane], cutoff: int, margin: int
) -> tuple[Pane | None, float, tuple[str, ...]]:
    """Best fuzzy name match, with near-tie ambiguity detection.

    Returns ``(winner, score, candidates)``:
      - a single clear winner above the cutoff -> ``(pane, score, ())``
      - an ambiguous near-tie (>=2 names within ``margin`` of the top, all
        >= ``cutoff``) -> ``(None, top_score, (name, ...))``
      - nothing above the cutoff -> ``(None, 0.0, ())``
    """
    scored = sorted(
        ((p, fuzz.ratio(token, p.name.lower())) for p in _named(panes)),
        key=lambda ps: ps[1],
        reverse=True,
    )
    above = [(p, s) for p, s in scored if s >= cutoff]
    if not above:
        return None, 0.0, ()
    best_p, best_s = above[0]
    near = [p for p, s in above if best_s - s <= margin]
    if len(near) > 1:
        return None, best_s, tuple(p.name for p in near)
    return best_p, best_s, ()


def _phonetic(token: str, panes: list[Pane]) -> Pane | None:
    primary = doublemetaphone(token)[0]
    if not primary:
        return None
    for p in _named(panes):
        if doublemetaphone(p.name)[0] == primary:
            return p
    return None


def resolve_pane_by_name(
    token: str, panes: list[Pane], *, fuzzy_cutoff: int = 82, ambiguity_margin: int = 5
) -> NameMatch:
    """Resolve a single spoken name token to a pane via the router cascade.

    exact (100) -> fuzzy (score, with near-tie ambiguity) -> phonetic (70).
    Returns a NameMatch; pane_id is None on no match or an ambiguous near-tie
    (candidates non-empty only in the ambiguous case).
    """
    token = token.strip(_STRIP).lower()
    if not token:
        return NameMatch(None, None, 0.0)
    hit = _exact(token, panes)
    if hit is not None:
        return NameMatch(hit.id, hit.name, 100.0, method="exact")
    hit, score, candidates = _fuzzy_match(token, panes, fuzzy_cutoff, ambiguity_margin)
    if candidates:
        return NameMatch(None, None, score, candidates)
    if hit is not None:
        return NameMatch(hit.id, hit.name, score, method="fuzzy")
    hit = _phonetic(token, panes)
    if hit is not None:
        return NameMatch(hit.id, hit.name, 70.0, method="metaphone")
    return NameMatch(None, None, 0.0)


def _number(token: str) -> int | None:
    n = word_to_int(token)
    return n if n is not None and 1 <= n <= 9 else None


def route(transcript: str, panes: list[Pane], focused_id: str | None,
          *, fuzzy_cutoff: int = 82, ambiguity_margin: int = 5) -> Route:
    token, remainder = _first_token(transcript)

    def fallback() -> Route:
        # Route to the focused pane (if any) without consuming the token.
        return Route(pane_id=focused_id, text=transcript, matched_name=None,
                     confidence=0.0, fallback=True, match_method="focus_fallback")

    if not token:
        return fallback()

    # 1-3. Name cascade (exact -> fuzzy/ambiguity -> phonetic), reused by commands.
    m = resolve_pane_by_name(
        token, panes, fuzzy_cutoff=fuzzy_cutoff, ambiguity_margin=ambiguity_margin)
    if m.candidates:
        return Route(pane_id=None, text=transcript, matched_name=None,
                     confidence=m.confidence, fallback=False, candidates=m.candidates)
    if m.pane_id is not None:
        return Route(pane_id=m.pane_id, text=remainder, matched_name=m.matched_name,
                     confidence=m.confidence, fallback=False, match_method=m.method)

    # 4. Number (digit or word 1..9) -> the n-th pane (1-based) in the FOCUSED
    # window, ordered by pane index. Position-based, NOT pane_index == n: tmux's
    # default pane-base-index is 0, so matching the raw index would be off by one
    # ("two" -> the 3rd pane). Ranking by sorted position is correct whether tmux
    # is 0- or 1-based (ensure_up pins it to 1 for display, but routing must not
    # depend on that). Indices within a window are contiguous, so this also lines
    # up with the displayed numbers.
    n = _number(token)
    if n is not None and focused_id is not None:
        focused = next((p for p in panes if p.id == focused_id), None)
        if focused is not None:
            siblings = sorted(
                (p for p in panes if p.window_id == focused.window_id),
                key=lambda p: p.index)
            if 1 <= n <= len(siblings):
                target = siblings[n - 1]
                return Route(pane_id=target.id, text=remainder, matched_name=None,
                             confidence=100.0, fallback=False, match_method="number")

    # 5. Vocative filler peel. No confident match on the raw leading token; if it
    # is a filler ("okay Atlas ..."), peel up to two fillers and retry an EXACT
    # name match ONLY. Fuzzy/phonetic/number are intentionally NOT retried, so a
    # common content word after a filler ("okay member ...", "okay two ...") can
    # never be mistaken for an address. On any miss we fall through to the focus
    # fallback below, which injects the ORIGINAL transcript verbatim.
    if token in _FILLERS:
        peeled, n = _peel_fillers(transcript)
        if n:
            ptoken, premainder = _first_token(peeled)
            hit = _exact(ptoken, panes)
            if hit is not None:
                return Route(pane_id=hit.id, text=premainder,
                             matched_name=hit.name, confidence=100.0, fallback=False,
                             match_method="exact")

    # else: focus fallback (text unchanged).
    return fallback()


# Curated callsigns auto-assigned to new panes: short, easy to say, and chosen
# to be mutually distinct under the router's fuzzy/phonetic matching so the ASR
# rarely confuses them. Assignment walks this list in order and skips any that
# collide with a name already in use.
CALLSIGNS: tuple[str, ...] = (
    "nova", "atlas", "sage", "echo", "orion", "river", "ember", "juno",
    "lyra", "vega", "koda", "slate", "raven", "quill", "tango", "pixel",
)


def next_callsign(used: list[str], *, fuzzy_cutoff: int = 82) -> str | None:
    """First CALLSIGN not confusable with any name in `used`, else None.

    Reuses :func:`name_collides` so an auto-assigned callsign can never clash
    with an existing pane name (exact, fuzzy, or phonetic).
    """
    for cand in CALLSIGNS:
        if name_collides(cand, used, fuzzy_cutoff=fuzzy_cutoff) is None:
            return cand
    return None


def name_collides(candidate: str, existing: list[str],
                  *, fuzzy_cutoff: int = 82) -> str | None:
    """Return an existing name confusable with `candidate`, else None.

    Used by `vupai name` to reject names a router would mis-route. A collision
    is an exact (case-insensitive) match, a rapidfuzz ratio >= cutoff, or an
    equal double-metaphone primary code.
    """
    cand = candidate.strip(_STRIP).lower()
    cand_code = doublemetaphone(cand)[0]
    for name in existing:
        # Skip pseudo-titles that tmux sets when no real name is assigned (%N).
        if name.startswith("%"):
            continue
        other = name.lower()
        if other == cand:
            return name
        if fuzz.ratio(cand, other) >= fuzzy_cutoff:
            return name
        if cand_code and doublemetaphone(other)[0] == cand_code:
            return name
    return None
