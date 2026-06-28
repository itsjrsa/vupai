from vupai import reviewtui


def _view(files, ledger):
    return {"tree": "/repo", "ledger": ledger, "files": files}


def _file(path, *, panes, conflict=False, status="M"):
    return {"path": path, "status": status, "added": 1, "deleted": 0,
            "binary": False, "panes": panes, "attributed": bool(panes),
            "conflict": conflict, "coverage": "git-delta" if panes else "none",
            "patch": ""}


def test_build_rows_groups_files_under_panes():
    files = [_file("a.py", panes=["sage"])]
    ledger = [{"pane": "sage", "files": ["a.py"], "coverage": "git-delta"}]
    rows = reviewtui.build_rows([_view(files, ledger)])
    assert rows[0] == {"kind": "pane", "pane": "sage", "coverage": "git-delta"}
    assert rows[1]["kind"] == "file" and rows[1]["record"]["path"] == "a.py"
    assert rows[1]["pane"] == "sage"


def test_build_rows_active_pane_when_no_changed_files():
    ledger = [{"pane": "ember", "files": [], "coverage": "churn-only"}]
    rows = reviewtui.build_rows([_view([], ledger)])
    assert rows[0]["kind"] == "pane" and rows[0]["pane"] == "ember"
    assert rows[1] == {"kind": "active", "pane": "ember"}


def test_build_rows_unattributed_bucket_last():
    files = [_file("orphan.py", panes=[])]
    rows = reviewtui.build_rows([_view(files, ledger=[])])
    assert rows[0] == {"kind": "sep", "label": "unattributed"}
    assert rows[1]["kind"] == "file" and rows[1]["pane"] is None


def test_build_rows_conflict_pane_first():
    files = [
        _file("calm.py", panes=["zeta"]),
        _file("hot.py", panes=["aria", "zeta"], conflict=True),
    ]
    ledger = [
        {"pane": "aria", "files": ["hot.py"], "coverage": "git-delta"},
        {"pane": "zeta", "files": ["calm.py", "hot.py"], "coverage": "git-delta"},
    ]
    rows = reviewtui.build_rows([_view(files, ledger)])
    pane_order = [r["pane"] for r in rows if r["kind"] == "pane"]
    assert pane_order[0] == "aria"  # conflict-first then name: aria < zeta
    # The conflict file sorts first within each pane group.
    first_file_under_first_pane = next(r for r in rows if r["kind"] == "file")
    assert first_file_under_first_pane["record"]["path"] == "hot.py"


def test_build_rows_folded_pane_hides_files():
    files = [_file("a.py", panes=["sage"])]
    ledger = [{"pane": "sage", "files": ["a.py"], "coverage": "git-delta"}]
    rows = reviewtui.build_rows([_view(files, ledger)], folded={"sage"})
    assert [r["kind"] for r in rows] == ["pane"]


def test_diff_lines_classifies_kinds():
    patch = ("diff --git a/x b/x\n"
             "--- a/x\n+++ b/x\n"
             "@@ -1,2 +1,2 @@\n"
             " ctx\n-gone\n+added\n")
    kinds = [k for k, _ in reviewtui.diff_lines(patch)]
    assert kinds == ["meta", "meta", "meta", "hunk", "ctx", "del", "add"]


def test_reselect_keeps_same_path():
    rows = reviewtui.build_rows(
        [_view([_file("a.py", panes=["s"]), _file("b.py", panes=["s"])],
               [{"pane": "s", "files": ["a.py", "b.py"], "coverage": "git-delta"}])])
    idx = reviewtui.reselect(rows, "b.py")
    assert rows[idx]["record"]["path"] == "b.py"


def test_move_selection_skips_non_file_rows():
    rows = reviewtui.build_rows(
        [_view([_file("a.py", panes=["s"])],
               [{"pane": "s", "files": ["a.py"], "coverage": "git-delta"}])]
        + [_view([_file("b.py", panes=["t"])],
                 [{"pane": "t", "files": ["b.py"], "coverage": "git-delta"}])])
    start = reviewtui.first_file_index(rows)
    nxt = reviewtui.move_selection(rows, start, 1)
    assert rows[nxt]["kind"] == "file" and rows[nxt]["record"]["path"] == "b.py"


def _state(rows, **over):
    base = {"views": [], "folded": set(), "rows": rows,
            "sel": reviewtui.first_file_index(rows), "diff_scroll": 3,
            "paused": False}
    base.update(over)
    return base


def _rows_one_file():
    return reviewtui.build_rows(
        [_view([_file("a.py", panes=["s"])],
               [{"pane": "s", "files": ["a.py"], "coverage": "git-delta"}])])


def test_step_quit():
    _, action = reviewtui.step(_state(_rows_one_file()), ord("q"))
    assert action == "quit"


def test_step_open():
    _, action = reviewtui.step(_state(_rows_one_file()), 10)
    assert action == "open"


def test_step_refresh():
    _, action = reviewtui.step(_state(_rows_one_file()), ord("r"))
    assert action == "refresh"


def test_step_toggle_pause():
    st, action = reviewtui.step(_state(_rows_one_file()), ord("p"))
    assert action is None and st["paused"] is True


def test_step_move_resets_diff_scroll():
    st, action = reviewtui.step(_state(_rows_one_file()), 258)  # KEY_DOWN
    assert action is None and st["diff_scroll"] == 0


class _FakeStdscr:
    """Minimal curses screen capturing drawn text, ignoring positioning."""

    def __init__(self, h=24, w=200):
        self._h, self._w = h, w
        self.drawn: list[str] = []

    def getmaxyx(self):
        return (self._h, self._w)

    def erase(self):
        self.drawn.clear()

    def addnstr(self, y, x, text, n, *attr):
        self.drawn.append(text[:n])

    def addstr(self, y, x, text, *attr):
        self.drawn.append(text)

    def hline(self, *a):
        pass

    def refresh(self):
        pass


def test_render_frame_draws_panes_and_diff_without_error():
    files = [_file("a.py", panes=["sage"])]
    files[0]["patch"] = "@@ -1 +1 @@\n-old\n+new\n"
    ledger = [{"pane": "sage", "files": ["a.py"], "coverage": "git-delta"}]
    rows = reviewtui.build_rows([_view(files, ledger)])
    state = {"views": [_view(files, ledger)], "folded": set(), "rows": rows,
             "sel": reviewtui.first_file_index(rows), "diff_scroll": 0,
             "paused": False}
    scr = _FakeStdscr()
    reviewtui.render_frame(scr, state)  # must not raise
    blob = "\n".join(scr.drawn)
    assert "sage" in blob
    assert "a.py" in blob
    assert "sage's changes (exact)" in blob  # single-author provenance header


def test_render_frame_header_shows_scoped_session():
    files = [_file("a.py", panes=["sage"])]
    ledger = [{"pane": "sage", "files": ["a.py"], "coverage": "git-delta"}]
    rows = reviewtui.build_rows([_view(files, ledger)])
    state = {"views": [_view(files, ledger)], "folded": set(), "rows": rows,
             "sel": reviewtui.first_file_index(rows), "diff_scroll": 0,
             "paused": False, "session": "backend"}
    scr = _FakeStdscr()
    reviewtui.render_frame(scr, state)
    blob = "\n".join(scr.drawn)
    assert "vupai review · backend" in blob  # which session is scoped


def test_render_frame_conflict_banner_for_multi_pane_file():
    files = [_file("hot.py", panes=["sage", "orion"], conflict=True)]
    ledger = [
        {"pane": "sage", "files": ["hot.py"], "coverage": "git-delta"},
        {"pane": "orion", "files": ["hot.py"], "coverage": "git-delta"},
    ]
    rows = reviewtui.build_rows([_view(files, ledger)])
    sel = next(i for i, r in enumerate(rows) if r["kind"] == "file")
    state = {"views": [_view(files, ledger)], "folded": set(), "rows": rows,
             "sel": sel, "diff_scroll": 0, "paused": False}
    scr = _FakeStdscr()
    reviewtui.render_frame(scr, state)
    blob = "\n".join(scr.drawn)
    assert "combined" in blob and "not splittable without worktrees" in blob


def test_ensure_selected_patch_loads_once_and_caches():
    calls = []

    def patch_fn(rec):
        calls.append(rec["path"])
        return "PATCH:" + rec["path"]

    files = [_file("a.py", panes=["s"])]
    files[0]["tree"] = "/repo"
    ledger = [{"pane": "s", "files": ["a.py"], "coverage": "git-delta"}]
    rows = reviewtui.build_rows([_view(files, ledger)])
    sel = reviewtui.first_file_index(rows)
    state = {"views": [_view(files, ledger)], "folded": set(), "rows": rows,
             "sel": sel, "diff_scroll": 0, "paused": False, "patch_cache": {}}
    reviewtui._ensure_selected_patch(state, patch_fn)
    reviewtui._ensure_selected_patch(state, patch_fn)  # second call hits cache
    assert calls == ["a.py"]                            # fetched exactly once
    assert rows[sel]["record"]["patch"] == "PATCH:a.py"


def test_step_space_folds_unattributed_bucket():
    files = [_file("orphan.py", panes=[])]
    rows = reviewtui.build_rows([_view(files, ledger=[])])
    sel = next(i for i, r in enumerate(rows) if r["kind"] == "file")
    state = {"views": [_view(files, ledger=[])], "folded": set(), "rows": rows,
             "sel": sel, "diff_scroll": 0, "paused": False, "patch_cache": {}}
    st, action = reviewtui.step(state, ord(" "))
    assert action is None
    assert "unattributed" in st["folded"]
    assert all(r["kind"] != "file" for r in st["rows"])  # file rows hidden


def test_cli_review_parser_registered():
    from vupai import cli
    parser = cli.build_parser()
    ns = parser.parse_args(["review"])
    assert ns.func is cli._cmd_review
