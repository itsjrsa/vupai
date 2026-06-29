"""Layer 2 Phase 1: headless data for `vupai review`. Read-only git joined to
the Layer 1 activity ledger. Never mutates the index, never injects."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from . import tmuxio
from .activity import ActivityStore, _excluded, git_toplevel

MAX_PATCH_BYTES = 200_000


def _self_cmd() -> str:
    """How to re-invoke this CLI from a tmux window (absolute interpreter,
    socket-prefixed) so the spawned review window queries vupai's own server.
    Mirrors board._self_cmd; the spoken 'open review' verb calls open_review
    with no self_cmd, so this fallback must carry the socket too."""
    return f"{tmuxio.socket_env_prefix()}{sys.executable} -m vupai"


def open_review(session: str, *, io=tmuxio,
                self_cmd: str | None = None) -> tuple[bool, str]:
    """Open (or focus) a full-window review TUI for `session`. One per session:
    if a review window already exists, focus it instead of opening a second.
    Shared by the `vupai review` CLI command and the spoken 'open review' verb."""
    existing = io.find_review_pane(session) if session else None
    if existing is not None:
        io.select_pane(existing)
        return False, "review already open in this session"
    inner = f"{self_cmd or _self_cmd()} _review {shlex.quote(session)}"
    pane_id = io.new_window(session, inner, name="review")
    io.mark_review_pane(pane_id)
    return True, "opened review"


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


def load_patch(rec: dict, *, git_fn=_run_git) -> str:
    """Fetch one file record's unified diff on demand. Reads rec['tree'] (set
    by collect_tree). Read-only; never mutates the index. Kept separate from
    collect_tree so a live caller fetches only the patch it is about to show,
    not one per changed file per poll."""
    return _file_patch(rec["tree"], rec, git_fn)


def collect_tree(tree: str, *, ledger: list[dict], git_fn=_run_git,
                 excludes: tuple = ()) -> dict:
    """Per-tree change set joined to attribution. Each record is tagged with
    its tree; patches are fetched lazily via load_patch, not here, so a live
    caller does not spawn one git diff per changed file on every poll."""
    status_out = git_fn(tree, ["status", "--porcelain", "-z"]) or ""
    numstat_out = git_fn(tree, ["diff", "HEAD", "--numstat", "-z"]) or ""
    changes = parse_status(status_out)
    counts = parse_numstat(numstat_out)
    records = build_file_records(changes, counts, ledger, excludes=excludes)
    for rec in records:
        rec["tree"] = tree
    return {"tree": tree, "files": records}


def gather_review(registry, *, session=None, cwd_fn=tmuxio.pane_current_path,
                  git_fn=_run_git, dir_name: str = ".vupai",
                  excludes: tuple = ()) -> list[dict]:
    """One tree view per distinct git root under the session's panes, each
    joining that tree's ledger snapshot to its live diff. Empty trees dropped."""
    registry.refresh()
    roots: dict[str, None] = {}
    for pane in registry.panes:
        if session and pane.session != session:
            continue
        cwd = cwd_fn(pane.id)
        if not cwd:
            continue
        root = git_toplevel(cwd, git_fn)
        if root:
            roots[root] = None
    # Only attribute to panes that are live right now: a ledger record for a
    # since-closed pane is stale and must not show up as an active editor.
    live = {pane.name for pane in registry.panes
            if not session or pane.session == session}
    views: list[dict] = []
    for root in roots:
        ledger = [
            rec for rec in ActivityStore(Path(root), dir_name=dir_name)
            .read_current().values()
            if (not session or rec.get("session") in (None, session))
            and rec.get("pane") in live
        ]
        view = collect_tree(root, ledger=ledger, git_fn=git_fn, excludes=excludes)
        if view["files"]:
            view["ledger"] = ledger
            views.append(view)
    return views
