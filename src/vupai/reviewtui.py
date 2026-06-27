"""Layer 2 Phase 1: curses master-detail TUI for `vupai review`. The pure core
(build_rows / diff_lines / step) carries the logic; render_frame and the loop
are the thin IO shell. Read-only: never writes a pane, never injects."""

from __future__ import annotations

import curses

_COVERAGE_RANK = {"exact": 3, "git-delta": 2, "churn-only": 1, "none": 0}


def _pane_files(files: list[dict], pane: str) -> list[dict]:
    fs = [f for f in files if pane in f["panes"]]
    fs.sort(key=lambda f: (not f["conflict"], f["path"]))
    return fs


def build_rows(views: list[dict], folded=frozenset()) -> list[dict]:
    """Flatten tree views into ordered display rows. Pane groups conflict-first
    then by name; files within a group conflict-first then by path; a pane with
    no changed files becomes an `active` row; unattributed files sit in a
    trailing bucket. Folded names omit their file rows."""
    rows: list[dict] = []
    for view in views:
        files = view["files"]
        panes: list[tuple] = []
        seen: set[str] = set()
        for rec in sorted(view.get("ledger", []), key=lambda r: r.get("pane") or ""):
            name = rec.get("pane")
            if not name or name in seen:
                continue
            seen.add(name)
            panes.append((name, rec.get("coverage", "none")))
        panes.sort(key=lambda nc: (
            not any(f["conflict"] for f in _pane_files(files, nc[0])), nc[0]))
        for name, coverage in panes:
            rows.append({"kind": "pane", "pane": name, "coverage": coverage})
            pf = _pane_files(files, name)
            if not pf:
                rows.append({"kind": "active", "pane": name})
                continue
            if name in folded:
                continue
            for f in pf:
                rows.append({"kind": "file", "record": f, "pane": name})
        unattributed = sorted(
            (f for f in files if not f["attributed"]), key=lambda f: f["path"])
        if unattributed:
            rows.append({"kind": "sep", "label": "unattributed"})
            if "unattributed" not in folded:
                for f in unattributed:
                    rows.append({"kind": "file", "record": f, "pane": None})
    return rows


def first_file_index(rows: list[dict]) -> int:
    for i, r in enumerate(rows):
        if r["kind"] == "file":
            return i
    return 0


def reselect(rows: list[dict], prev_path: str | None) -> int:
    for i, r in enumerate(rows):
        if r["kind"] == "file" and r["record"]["path"] == prev_path:
            return i
    return first_file_index(rows)


def move_selection(rows: list[dict], idx: int, delta: int) -> int:
    i = idx + delta
    while 0 <= i < len(rows):
        if rows[i]["kind"] == "file":
            return i
        i += delta
    return idx


def diff_lines(patch: str) -> list[tuple]:
    out: list[tuple] = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            out.append(("hunk", line))
        elif (line.startswith("+++") or line.startswith("---")
              or line.startswith("diff ") or line.startswith("index ")
              or line.startswith("new file") or line.startswith("deleted file")
              or line.startswith("rename ")):
            out.append(("meta", line))
        elif line.startswith("+"):
            out.append(("add", line))
        elif line.startswith("-"):
            out.append(("del", line))
        else:
            out.append(("ctx", line))
    return out


def _pane_of(rows: list[dict], idx: int) -> str | None:
    if 0 <= idx < len(rows):
        r = rows[idx]
        if r["kind"] in ("pane", "active"):
            return r["pane"]
        if r["kind"] == "file":
            return r["pane"]
    return None


def _selected_path(state: dict) -> str | None:
    rows, sel = state["rows"], state["sel"]
    if 0 <= sel < len(rows) and rows[sel]["kind"] == "file":
        return rows[sel]["record"]["path"]
    return None


def step(state: dict, key: int) -> tuple[dict, str | None]:
    """Pure input reducer. Returns (new_state, action)."""
    if key in (ord("q"), 27):
        return state, "quit"
    if key in (10, 13, ord("o")):
        return state, "open"
    if key == ord("r"):
        return state, "refresh"
    if key == ord("p"):
        state["paused"] = not state["paused"]
        return state, None
    if key in (ord("j"), curses.KEY_DOWN):
        state["sel"] = move_selection(state["rows"], state["sel"], 1)
        state["diff_scroll"] = 0
        return state, None
    if key in (ord("k"), curses.KEY_UP):
        state["sel"] = move_selection(state["rows"], state["sel"], -1)
        state["diff_scroll"] = 0
        return state, None
    if key == ord(" "):
        pane = _pane_of(state["rows"], state["sel"])
        if pane:
            state["folded"] ^= {pane}
            prev = _selected_path(state)
            state["rows"] = build_rows(state["views"], state["folded"])
            state["sel"] = reselect(state["rows"], prev)
            state["diff_scroll"] = 0
        return state, None
    if key == curses.KEY_NPAGE:
        state["diff_scroll"] += 1
        return state, None
    if key == curses.KEY_PPAGE:
        state["diff_scroll"] = max(0, state["diff_scroll"] - 1)
        return state, None
    return state, None
