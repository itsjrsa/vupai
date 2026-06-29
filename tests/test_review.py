from vupai import activity, review
from vupai.registry import PaneRegistry


def test_parse_numstat_normal_and_binary():
    out = "5\t2\tsrc/a.py\x00-\t-\tlogo.png\x00"
    assert review.parse_numstat(out) == {
        "src/a.py": {"added": 5, "deleted": 2, "binary": False},
        "logo.png": {"added": 0, "deleted": 0, "binary": True},
    }


def test_parse_numstat_rename_uses_new_path():
    # -z rename: header has an empty path field, then old NUL new.
    out = "3\t1\t\x00old/x.py\x00new/y.py\x00"
    assert review.parse_numstat(out) == {
        "new/y.py": {"added": 3, "deleted": 1, "binary": False},
    }


def test_parse_numstat_empty():
    assert review.parse_numstat("") == {}


def test_parse_status_letters_and_untracked():
    out = " M src/a.py\x00A  src/b.py\x00?? notes.md\x00"
    assert review.parse_status(out) == [
        {"path": "src/a.py", "status": "M"},
        {"path": "src/b.py", "status": "A"},
        {"path": "notes.md", "status": "?"},
    ]


def test_parse_status_rename_consumes_origin_token():
    # -z rename entry: "R  <new>" then a separate NUL token for the old name.
    out = "R  new/y.py\x00old/x.py\x00 M other.py\x00"
    assert review.parse_status(out) == [
        {"path": "new/y.py", "status": "R"},
        {"path": "other.py", "status": "M"},
    ]


def test_parse_status_empty():
    assert review.parse_status("") == []


def test_build_file_records_attributes_and_counts():
    changes = [{"path": "src/router.py", "status": "M"}]
    counts = {"src/router.py": {"added": 42, "deleted": 8, "binary": False}}
    ledger = [{"pane": "sage", "files": ["src/router.py"], "coverage": "git-delta"}]
    recs = review.build_file_records(changes, counts, ledger)
    assert recs == [{
        "path": "src/router.py", "status": "M", "added": 42, "deleted": 8,
        "binary": False, "panes": ["sage"], "attributed": True,
        "conflict": False, "coverage": "git-delta"}]


def test_build_file_records_flags_conflict_and_picks_strongest_coverage():
    changes = [{"path": "src/app.py", "status": "M"}]
    counts = {"src/app.py": {"added": 1, "deleted": 0, "binary": False}}
    ledger = [
        {"pane": "sage", "files": ["src/app.py"], "coverage": "git-delta"},
        {"pane": "orion", "files": ["src/app.py"], "coverage": "exact"},
    ]
    rec = review.build_file_records(changes, counts, ledger)[0]
    assert rec["panes"] == ["orion", "sage"]  # sorted
    assert rec["conflict"] is True
    assert rec["coverage"] == "exact"  # strongest of the two


def test_build_file_records_unattributed_when_no_pane_claims_path():
    changes = [{"path": "notes.md", "status": "?"}]
    counts = {}
    rec = review.build_file_records(changes, counts, ledger=[])[0]
    assert rec["attributed"] is False
    assert rec["panes"] == []
    assert rec["coverage"] == "none"
    assert rec["added"] == 0 and rec["deleted"] == 0 and rec["binary"] is False


def test_build_file_records_sorts_conflict_first_then_unattributed_last():
    changes = [
        {"path": "z_attr.py", "status": "M"},
        {"path": "a_unattr.py", "status": "M"},
        {"path": "m_conflict.py", "status": "M"},
    ]
    counts = {}
    ledger = [
        {"pane": "sage", "files": ["z_attr.py", "m_conflict.py"], "coverage": "git-delta"},
        {"pane": "orion", "files": ["m_conflict.py"], "coverage": "git-delta"},
    ]
    order = [r["path"] for r in review.build_file_records(changes, counts, ledger)]
    assert order == ["m_conflict.py", "z_attr.py", "a_unattr.py"]


def test_build_file_records_drops_excluded_paths():
    changes = [{"path": "uv.lock", "status": "M"}, {"path": "src/a.py", "status": "M"}]
    counts = {}
    recs = review.build_file_records(changes, counts, ledger=[], excludes=("uv.lock",))
    assert [r["path"] for r in recs] == ["src/a.py"]


def _git_fixture(responses):
    """responses: dict mapping a stringified args list to stdout. Returns a
    fake git_fn accepting the same (tree, args, **kwargs) shape as _run_git."""
    def fake_git(tree, args, **kwargs):
        return responses.get(" ".join(args))
    return fake_git


def test_collect_tree_tags_tree_and_defers_patch():
    git = _git_fixture({
        "status --porcelain -z": " M src/a.py\x00",
        "diff HEAD --numstat -z": "5\t2\tsrc/a.py\x00",
    })
    ledger = [{"pane": "sage", "files": ["src/a.py"], "coverage": "git-delta"}]
    view = review.collect_tree("/repo", ledger=ledger, git_fn=git)
    assert view["tree"] == "/repo"
    f = view["files"][0]
    assert f["path"] == "src/a.py" and f["panes"] == ["sage"]
    assert f["tree"] == "/repo"          # tagged for lazy load
    assert "patch" not in f              # patch is no longer eager


def test_load_patch_tracked():
    git = _git_fixture({"diff HEAD -- src/a.py": "@@ -1 +1 @@\n-old\n+new\n"})
    rec = {"tree": "/repo", "path": "src/a.py", "status": "M", "binary": False}
    assert review.load_patch(rec, git_fn=git) == "@@ -1 +1 @@\n-old\n+new\n"


def test_load_patch_untracked_uses_no_index():
    git = _git_fixture({
        "diff --no-index -- /dev/null notes.md": "@@ -0,0 +1 @@\n+hello\n"})
    rec = {"tree": "/repo", "path": "notes.md", "status": "?", "binary": False}
    assert review.load_patch(rec, git_fn=git) == "@@ -0,0 +1 @@\n+hello\n"


def test_load_patch_binary_is_empty():
    rec = {"tree": "/repo", "path": "logo.png", "status": "M", "binary": True}
    assert review.load_patch(rec, git_fn=lambda *a, **k: "ignored") == ""


def test_load_patch_caps_large_patch():
    big = "+x\n" * 200_000
    git = _git_fixture({"diff HEAD -- big.py": big})
    rec = {"tree": "/repo", "path": "big.py", "status": "M", "binary": False}
    p = review.load_patch(rec, git_fn=git)
    assert len(p) <= review.MAX_PATCH_BYTES + 32
    assert p.endswith("... (truncated)\n")


def test_collect_tree_no_changes_returns_empty_files():
    git = _git_fixture({"status --porcelain -z": "", "diff HEAD --numstat -z": ""})
    assert review.collect_tree("/repo", ledger=[], git_fn=git) == {
        "tree": "/repo", "files": []}


def _pane_line(pid, name, *, session="proj", command="claude"):
    # tmuxio.PANE_FORMAT order: id, window_id, window, index, name, command,
    # active, session
    return "\t".join([pid, "@1", "win", "0", name, command, "1", session])


def test_gather_review_joins_ledger_and_diff_for_session(tmp_path):
    root = str(tmp_path)
    store = activity.ActivityStore(tmp_path)
    store.write_current({"sage": {
        "pane": "sage", "session": "proj", "tree": root,
        "files": ["src/a.py"], "coverage": "git-delta", "contended_with": []}})

    reg = PaneRegistry(
        lister=lambda: [_pane_line("%1", "sage", session="proj")],
        focuser=lambda: None)

    def fake_git(tree, args, **kwargs):
        if args[:1] == ["rev-parse"]:
            return root + "\n"
        if args == ["status", "--porcelain", "-z"]:
            return " M src/a.py\x00"
        if args == ["diff", "HEAD", "--numstat", "-z"]:
            return "5\t2\tsrc/a.py\x00"
        if args[:2] == ["diff", "HEAD"]:
            return "@@ -1 +1 @@\n-old\n+new\n"
        return None

    views = review.gather_review(
        reg, session="proj", cwd_fn=lambda pid: root, git_fn=fake_git)
    assert len(views) == 1
    assert views[0]["tree"] == root
    assert [r["pane"] for r in views[0]["ledger"]] == ["sage"]
    assert views[0]["files"][0]["path"] == "src/a.py"
    assert views[0]["files"][0]["panes"] == ["sage"]


def test_gather_review_skips_trees_with_no_changes(tmp_path):
    root = str(tmp_path)
    reg = PaneRegistry(
        lister=lambda: [_pane_line("%1", "sage", session="proj")],
        focuser=lambda: None)

    def fake_git(tree, args, **kwargs):
        if args[:1] == ["rev-parse"]:
            return root + "\n"
        return ""  # clean tree: no status, no numstat

    views = review.gather_review(
        reg, session="proj", cwd_fn=lambda pid: root, git_fn=fake_git)
    assert views == []


def test_gather_review_drops_records_for_closed_panes(tmp_path):
    # The ledger still holds a record for "ghost", a pane that has since been
    # closed (it is not in the live registry). gather_review must omit it, so a
    # file only that dead pane touched is not shown nor flagged as a conflict.
    root = str(tmp_path)
    store = activity.ActivityStore(tmp_path)
    store.write_current({
        "sage": {"pane": "sage", "session": "proj", "tree": root,
                 "files": ["src/a.py"], "coverage": "git-delta",
                 "contended_with": []},
        "ghost": {"pane": "ghost", "session": "proj", "tree": root,
                  "files": ["src/a.py"], "coverage": "git-delta",
                  "contended_with": []},
    })
    reg = PaneRegistry(  # only sage is live now; ghost is gone
        lister=lambda: [_pane_line("%1", "sage", session="proj")],
        focuser=lambda: None)

    def fake_git(tree, args, **kwargs):
        if args[:1] == ["rev-parse"]:
            return root + "\n"
        if args == ["status", "--porcelain", "-z"]:
            return " M src/a.py\x00"
        if args == ["diff", "HEAD", "--numstat", "-z"]:
            return "5\t2\tsrc/a.py\x00"
        return None

    views = review.gather_review(
        reg, session="proj", cwd_fn=lambda pid: root, git_fn=fake_git)
    assert len(views) == 1
    assert [r["pane"] for r in views[0]["ledger"]] == ["sage"]
    f = views[0]["files"][0]
    assert f["panes"] == ["sage"]      # not attributed to the dead ghost pane
    assert f["conflict"] is False      # so no false cross-pane conflict


class _ReviewIo:
    """Records the tmux calls open_review makes."""

    def __init__(self, existing=None, new_pane="%9"):
        self.calls = []
        self._existing = existing
        self._new_pane = new_pane

    def find_review_pane(self, session):
        self.calls.append(("find", session))
        return self._existing

    def select_pane(self, pane_id):
        self.calls.append(("select", pane_id))

    def new_window(self, session, program, *, name=None):
        self.calls.append(("new_window", session, program, name))
        return self._new_pane

    def mark_review_pane(self, pane_id):
        self.calls.append(("mark", pane_id))


def test_open_review_opens_window_when_none_exists():
    io = _ReviewIo(existing=None, new_pane="%9")
    opened, _msg = review.open_review("proj", io=io, self_cmd="PY -m vupai")
    assert opened is True
    win = next(c for c in io.calls if c[0] == "new_window")
    assert win[1] == "proj"                       # session
    assert win[2] == "PY -m vupai _review proj"    # inner command
    assert win[3] == "review"                      # window name
    assert ("mark", "%9") in io.calls


def test_open_review_focuses_existing_instead_of_second():
    io = _ReviewIo(existing="%5")
    opened, _msg = review.open_review("proj", io=io, self_cmd="PY")
    assert opened is False
    assert ("select", "%5") in io.calls
    assert not any(c[0] == "new_window" for c in io.calls)


def test_open_review_quotes_session_with_spaces():
    io = _ReviewIo(existing=None)
    review.open_review("my proj", io=io, self_cmd="PY")
    win = next(c for c in io.calls if c[0] == "new_window")
    assert win[2] == "PY _review 'my proj'"        # shlex.quote
