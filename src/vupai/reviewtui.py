"""Layer 2 Phase 1: curses master-detail TUI for `vupai review`. The pure core
(build_rows / diff_lines / step) carries the logic; render_frame and the loop
are the thin IO shell. Read-only: never writes a pane, never injects."""

from __future__ import annotations

import curses
import os
import subprocess


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
        if r["kind"] in ("pane", "active", "file"):
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


# Curses color pair ids (initialized in _init_colors).
_CP_ADD, _CP_DEL, _CP_HUNK, _CP_META, _CP_CONFLICT, _CP_DIM, _CP_HEADER = range(1, 8)


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_CP_ADD, curses.COLOR_GREEN, -1)
    curses.init_pair(_CP_DEL, curses.COLOR_RED, -1)
    curses.init_pair(_CP_HUNK, curses.COLOR_CYAN, -1)
    curses.init_pair(_CP_META, curses.COLOR_BLUE, -1)
    curses.init_pair(_CP_CONFLICT, curses.COLOR_RED, -1)
    curses.init_pair(_CP_DIM, curses.COLOR_YELLOW, -1)
    curses.init_pair(_CP_HEADER, curses.COLOR_WHITE, -1)


def _attr(pair: int) -> int:
    try:
        return curses.color_pair(pair)
    except curses.error:
        return 0


_KIND_ATTR = {"add": _CP_ADD, "del": _CP_DEL, "hunk": _CP_HUNK, "meta": _CP_META}


def _counts(views: list[dict]) -> tuple[int, int, int]:
    panes, files, conflicts = set(), 0, 0
    for v in views:
        for rec in v.get("ledger", []):
            if rec.get("pane"):
                panes.add(rec["pane"])
        for f in v["files"]:
            files += 1
            if f["conflict"]:
                conflicts += 1
    return len(panes), files, conflicts


def _file_label(rec: dict) -> str:
    mark = "! " if rec["conflict"] else "  "
    counts = "" if rec["binary"] else f"  +{rec['added']} -{rec['deleted']}"
    return f"{mark}{rec['status']} {rec['path']}{counts}"


def render_frame(stdscr, state) -> None:
    """Draw header, left list, right diff, footer. Tolerant of tiny screens."""
    rows = state["rows"]
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    left_w = max(24, w // 2)

    def put(y, x, text, attr=0, width=None):
        try:
            stdscr.addnstr(y, x, text, width if width is not None else max(0, w - x), attr)
        except curses.error:
            pass

    np, nf, nc = _counts(state["views"])
    live = "paused" if state["paused"] else "live"
    put(0, 0, f" vupai review   {live}   {np} panes - {nf} files - {nc} conflict",
        _attr(_CP_HEADER) | curses.A_BOLD)

    top = 1
    body_h = max(1, h - 2)
    # Left: scroll the list so the selection stays visible.
    sel = state["sel"]
    start = max(0, min(sel - body_h // 2, max(0, len(rows) - body_h)))
    for i in range(start, min(len(rows), start + body_h)):
        y = top + (i - start)
        r = rows[i]
        if r["kind"] == "pane":
            put(y, 0, f"v {r['pane']}  [{r['coverage']}]", curses.A_BOLD, left_w)
        elif r["kind"] == "active":
            put(y, 2, "- active, files unknown", _attr(_CP_DIM), left_w - 2)
        elif r["kind"] == "sep":
            put(y, 0, f"- {r['label']} -", _attr(_CP_DIM), left_w)
        else:
            rec = r["record"]
            attr = _attr(_CP_CONFLICT) if rec["conflict"] else 0
            if i == sel:
                attr |= curses.A_REVERSE
            put(y, 1, _file_label(rec), attr, left_w - 1)

    # Right: a provenance header (who this diff belongs to) + the diff itself.
    sc = state["diff_scroll"]
    if 0 <= sel < len(rows) and rows[sel]["kind"] == "file":
        rec = rows[sel]["record"]
        pane = rows[sel]["pane"]
        if rec["conflict"]:
            others = ", ".join(p for p in rec["panes"] if p != pane) or "another pane"
            header = f"combined - also edited by {others} (not splittable without worktrees)"
            head_attr = _attr(_CP_CONFLICT) | curses.A_BOLD
        elif pane:
            header = f"{pane}'s changes (exact)"
            head_attr = _attr(_CP_DIM)
        else:
            header = "unattributed change"
            head_attr = _attr(_CP_DIM)
        put(top, left_w + 1, header, head_attr, w - left_w - 1)
        diff = diff_lines(rec.get("patch", ""))
        for j in range(sc, min(len(diff), sc + (body_h - 1))):
            kind, text = diff[j]
            y = top + 1 + (j - sc)
            put(y, left_w + 1, text, _attr(_KIND_ATTR.get(kind, _CP_DIM)), w - left_w - 1)

    put(h - 1, 0,
        " up/down move - enter open - space fold - p pause - r refresh - q quit",
        _attr(_CP_DIM))
    stdscr.refresh()


def _open_in_editor(stdscr, state) -> None:
    path = _selected_path(state)
    if not path:
        return
    editor = os.environ.get("EDITOR", "vi")
    try:
        curses.endwin()
        subprocess.run([editor, path], check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        stdscr.refresh()
        curses.doupdate()


def _regather(state: dict, gather) -> dict:
    prev = _selected_path(state)
    views = gather()
    state["views"] = views
    state["rows"] = build_rows(views, state["folded"])
    state["sel"] = reselect(state["rows"], prev)
    return state


def _loop(stdscr, gather, interval: float) -> None:
    curses.curs_set(0)
    stdscr.timeout(int(interval * 1000))
    _init_colors()
    views = gather()
    state = {"views": views, "folded": set(),
             "rows": build_rows(views, set()), "sel": 0,
             "diff_scroll": 0, "paused": False}
    state["sel"] = first_file_index(state["rows"])
    while True:
        render_frame(stdscr, state)
        key = stdscr.getch()
        if key == -1:  # timeout tick -> live refresh
            if not state["paused"]:
                state = _regather(state, gather)
            continue
        state, action = step(state, key)
        if action == "quit":
            return
        if action == "refresh":
            state = _regather(state, gather)
        elif action == "open":
            _open_in_editor(stdscr, state)


def run_review_tui(gather, *, interval: float = 2.0) -> None:
    """Run the master-detail review TUI until the user quits. `gather` is a
    zero-arg callable returning the current list of tree views."""
    curses.wrapper(_loop, gather, interval)
