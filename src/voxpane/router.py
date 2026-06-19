from __future__ import annotations

from dataclasses import dataclass

from metaphone import doublemetaphone
from rapidfuzz import fuzz

from voxpane.registry import Pane

# Spoken number words for 1..9 -> pane index within the focused window.
_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}

# Characters stripped from the leading token before comparison (ASR punctuation).
_STRIP = ".,!?;:'\"()[]{}"


@dataclass(frozen=True)
class Route:
    pane_id: str | None   # None if no focus and no match, or on an ambiguous near-tie
    text: str             # transcript with any matched leading name token stripped
    matched_name: str | None
    confidence: float     # 0..100; 100 exact, rapidfuzz score for fuzzy, 0 for focus fallback
    fallback: bool        # True when routed to focused pane (no confident name match)
    candidates: tuple[str, ...] = ()  # non-empty only on an ambiguous near-tie name match


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


def _number(token: str) -> int | None:
    if token.isdigit():
        n = int(token)
    elif token in _NUMBER_WORDS:
        n = _NUMBER_WORDS[token]
    else:
        return None
    return n if 1 <= n <= 9 else None


def route(transcript: str, panes: list[Pane], focused_id: str | None,
          *, fuzzy_cutoff: int = 82, ambiguity_margin: int = 5) -> Route:
    token, remainder = _first_token(transcript)

    def fallback() -> Route:
        # Route to the focused pane (if any) without consuming the token.
        return Route(pane_id=focused_id, text=transcript, matched_name=None,
                     confidence=0.0, fallback=True)

    if not token:
        return fallback()

    # 1. Exact (case-insensitive) name match.
    hit = _exact(token, panes)
    if hit is not None:
        return Route(pane_id=hit.id, text=remainder, matched_name=hit.name,
                     confidence=100.0, fallback=False)

    # 2. Fuzzy name match (with near-tie ambiguity detection).
    hit, score, candidates = _fuzzy_match(token, panes, fuzzy_cutoff, ambiguity_margin)
    if candidates:
        # Two or more names are too close to call: surface them, route nowhere,
        # and leave the transcript intact so the user can re-say it.
        return Route(pane_id=None, text=transcript, matched_name=None,
                     confidence=score, fallback=False, candidates=candidates)
    if hit is not None:
        return Route(pane_id=hit.id, text=remainder, matched_name=hit.name,
                     confidence=score, fallback=False)

    # 3. Phonetic match via double-metaphone primary code.
    hit = _phonetic(token, panes)
    if hit is not None:
        return Route(pane_id=hit.id, text=remainder, matched_name=hit.name,
                     confidence=70.0, fallback=False)

    # 4. Number (digit or word 1..9) -> pane_index within the FOCUSED window.
    n = _number(token)
    if n is not None and focused_id is not None:
        focused = next((p for p in panes if p.id == focused_id), None)
        if focused is not None:
            target = next((p for p in panes
                           if p.window_id == focused.window_id and p.index == n), None)
            if target is not None:
                return Route(pane_id=target.id, text=remainder, matched_name=None,
                             confidence=100.0, fallback=False)

    # else: focus fallback (text unchanged).
    return fallback()


def name_collides(candidate: str, existing: list[str],
                  *, fuzzy_cutoff: int = 82) -> str | None:
    """Return an existing name confusable with `candidate`, else None.

    Used by `voxpane name` to reject names a router would mis-route. A collision
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
