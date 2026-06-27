"""Layer 2 Phase 1: headless data for `vupai review`. Read-only git joined to
the Layer 1 activity ledger. Never mutates the index, never injects."""

from __future__ import annotations


def parse_numstat(out: str) -> dict[str, dict]:
    """Parse `git diff HEAD --numstat -z`. Path -> added/deleted/binary.
    Binary files show `-` counts; renames carry an empty header path field
    followed by old NUL new (the new name wins)."""
    result: dict[str, dict] = {}
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        parts = tok.split("\t")
        if len(parts) < 3:
            i += 1
            continue
        added_s, deleted_s, path = parts[0], parts[1], parts[2]
        if path == "":
            old = tokens[i + 1] if i + 1 < len(tokens) else ""
            new = tokens[i + 2] if i + 2 < len(tokens) else ""
            path = new or old
            i += 3
        else:
            i += 1
        binary = added_s == "-" or deleted_s == "-"
        added = 0 if binary else int(added_s)
        deleted = 0 if binary else int(deleted_s)
        result[path] = {"added": added, "deleted": deleted, "binary": binary}
    return result


def _status_letter(xy: str) -> str:
    if xy == "??":
        return "?"
    for ch in xy:
        if ch in "ADRM":
            return ch
    return "M"


def parse_status(out: str) -> list[dict]:
    """Parse `git status --porcelain -z` into [{path, status}]. Rename/copy
    entries are followed by a separate NUL token for the original name, which
    is consumed and discarded."""
    result: list[dict] = []
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if len(tok) < 3:
            i += 1
            continue
        xy = tok[:2]
        path = tok[3:]
        i += 1
        if "R" in xy or "C" in xy:
            i += 1  # skip the origin-path token
        result.append({"path": path, "status": _status_letter(xy)})
    return result
