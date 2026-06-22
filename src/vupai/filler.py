"""Remove non-lexical filler tokens (um, uh, ...) from a transcript.

Pure and stateless: the only entry point is strip_fillers. The caller owns the
word set (see Config.filler_words) and the on/off decision.
"""

from __future__ import annotations

import re

# A token is a maximal run of word characters or apostrophes. Everything else
# (spaces, punctuation) is preserved verbatim between tokens so we can rebuild
# the string and only excise the fillers.
_TOKEN = re.compile(r"[\w']+|[^\w']+", re.UNICODE)


def _is_filler(token: str, words: frozenset[str]) -> bool:
    """True if token matches a filler word, tolerating a repeated final letter.

    "um" matches "um"/"umm"/"ummm" (the trailing run collapses to one). Match is
    case-insensitive and anchored to the whole token (callers pass single
    tokens), so substrings like "umbrella" never match.
    """
    lowered = token.lower()
    if lowered in words:
        return True
    # Collapse a trailing run of the same letter to a single letter and retry,
    # so elongated fillers (ummm, uhh) match without enumerating every length.
    collapsed = re.sub(r"(.)\1+$", r"\1", lowered)
    return collapsed in words


def strip_fillers(text: str, words: frozenset[str]) -> str:
    """Return text with standalone filler tokens removed and spacing repaired."""
    if not text or not words:
        return text
    kept: list[str] = []
    # Track if we removed a filler from the very beginning (before any other token).
    removed_leading_filler = False
    for i, piece in enumerate(_TOKEN.findall(text)):
        is_word = bool(re.match(r"[\w']", piece))
        if is_word and _is_filler(piece, words):
            if i == 0:
                removed_leading_filler = True
            continue
        kept.append(piece)
    result = "".join(kept)
    # Collapse runs of whitespace left by removed tokens, then trim stray
    # leading punctuation/space (a removed leading "Um, " leaves ", rest").
    result = re.sub(r"\s+", " ", result)
    result = re.sub(r"^[\s,;:.!?-]+", "", result).strip()
    if not result:
        return ""
    # Re-capitalize the first surviving letter if a filler was removed from
    # the very beginning of the text.
    if removed_leading_filler:
        return result[0].upper() + result[1:]
    return result
