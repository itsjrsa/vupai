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
    """True if token matches a filler word, tolerating elongation of the final
    letter ("um" matches umm/ummm; "hmm" matches hmmm; "mm" matches mmm). Match
    is case-insensitive and anchored to the whole token, so substrings like
    "umbrella" never match.
    """
    lowered = token.lower()
    if lowered in words:
        return True
    for word in words:
        if not word:
            continue
        # Allow zero or more extra repetitions of the word's final letter.
        if re.fullmatch(re.escape(word) + re.escape(word[-1]) + "*", lowered):
            return True
    return False


def strip_fillers(text: str, words: frozenset[str]) -> str:
    """Return text with standalone filler tokens removed and spacing repaired."""
    if not text or not words:
        # Empty word set is a no-op: nothing to strip.
        return text
    kept: list[str] = []
    # Track if we removed a filler before any word token was kept (leading filler).
    removed_leading_filler = False
    kept_any_word = False
    for piece in _TOKEN.findall(text):
        is_word = bool(re.match(r"[\w']", piece))
        if is_word and _is_filler(piece, words):
            # Filler removed before any word token was kept: mark for re-capitalization.
            if not kept_any_word:
                removed_leading_filler = True
            continue
        kept.append(piece)
        if is_word:
            kept_any_word = True
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
