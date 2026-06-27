"""Layer 2 Phase 1: headless data for `vupai review`. Read-only git joined to
the Layer 1 activity ledger. Never mutates the index, never injects."""

from __future__ import annotations

import subprocess

from .activity import _excluded

MAX_PATCH_BYTES = 200_000


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


_COVERAGE_RANK = {"exact": 3, "git-delta": 2, "churn-only": 1, "none": 0}


def build_file_records(changes: list[dict], counts: dict,
                       ledger: list[dict], *, excludes: tuple = ()) -> list[dict]:
    """Join git-changed files (truth) to ledger attribution. Conflict files
    (2+ panes) first, unattributed last, then by path. Never fabricates."""
    attrib: dict[str, list[tuple]] = {}
    for rec in ledger:
        cov = rec.get("coverage", "none")
        for f in rec.get("files") or []:
            attrib.setdefault(f, []).append((rec.get("pane"), cov))
    records: list[dict] = []
    for ch in changes:
        path = ch["path"]
        if _excluded(path, excludes):
            continue
        cnt = counts.get(path, {"added": 0, "deleted": 0, "binary": False})
        hits = attrib.get(path, [])
        panes = sorted({p for p, _ in hits if p})
        coverage = "none"
        if hits:
            coverage = max(
                (c for _, c in hits), key=lambda c: _COVERAGE_RANK.get(c, 0))
        records.append({
            "path": path, "status": ch["status"],
            "added": cnt["added"], "deleted": cnt["deleted"],
            "binary": cnt["binary"], "panes": panes,
            "attributed": bool(panes), "conflict": len(panes) >= 2,
            "coverage": coverage,
        })
    records.sort(key=lambda r: (not r["conflict"], not r["attributed"], r["path"]))
    return records


def _run_git(tree: str, args: list[str], *, ok_codes: tuple = (0,),
             timeout: float = 2.0) -> str | None:
    """Run `git -C <tree> <args>` read-only. Returns stdout, or None on a
    disallowed exit code / OSError / timeout. `ok_codes` lets `diff --no-index`
    (which exits 1 when files differ) count as success."""
    try:
        proc = subprocess.run(
            ["git", "-C", tree, *args],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in ok_codes:
        return None
    return proc.stdout


def _file_patch(tree: str, rec: dict, git_fn) -> str:
    if rec["binary"]:
        return ""
    if rec["status"] == "?":
        out = git_fn(tree, ["diff", "--no-index", "--", "/dev/null", rec["path"]],
                     ok_codes=(0, 1))
    else:
        out = git_fn(tree, ["diff", "HEAD", "--", rec["path"]])
    out = out or ""
    if len(out) > MAX_PATCH_BYTES:
        out = out[:MAX_PATCH_BYTES] + "\n... (truncated)\n"
    return out


def collect_tree(tree: str, *, ledger: list[dict], git_fn=_run_git,
                 excludes: tuple = ()) -> dict:
    """Per-tree change set joined to attribution, each file's patch attached."""
    status_out = git_fn(tree, ["status", "--porcelain", "-z"]) or ""
    numstat_out = git_fn(tree, ["diff", "HEAD", "--numstat", "-z"]) or ""
    changes = parse_status(status_out)
    counts = parse_numstat(numstat_out)
    records = build_file_records(changes, counts, ledger, excludes=excludes)
    for rec in records:
        rec["patch"] = _file_patch(tree, rec, git_fn)
    return {"tree": tree, "files": records}
