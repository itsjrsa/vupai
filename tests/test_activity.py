from vupai import activity
from vupai.registry import PaneRegistry


def test_extract_paths_finds_relative_and_nested_paths():
    tail = "edited src/vupai/router.py and tests/test_router.py ok"
    assert activity.extract_paths(tail) == {
        "src/vupai/router.py", "tests/test_router.py"}


def test_extract_paths_rejects_version_numbers_and_bare_numbers():
    tail = "claude v2.1.193 codex 0.142.2 took 3.5 seconds"
    assert activity.extract_paths(tail) == set()


def test_extract_paths_strips_leading_dot_slash_and_surrounding_text():
    tail = "see ./pkg/app.ts (modified) and `lib/util.py`."
    assert activity.extract_paths(tail) == {"pkg/app.ts", "lib/util.py"}


def test_extract_paths_keeps_bare_filename_with_alpha_extension():
    assert activity.extract_paths("touched README.md here") == {"README.md"}


def test_parse_porcelain_extracts_paths_and_handles_rename():
    out = (
        " M src/vupai/router.py\n"
        "?? newfile.py\n"
        "R  old/name.py -> src/new/name.py\n"
    )
    assert activity.parse_porcelain(out) == {
        "src/vupai/router.py", "newfile.py", "src/new/name.py"}


def test_parse_porcelain_empty():
    assert activity.parse_porcelain("") == set()


def test_compute_delta_flags_new_and_touched_files():
    prev = {"a.py": 100.0, "b.py": 100.0}
    cur = {"a.py": 100.0, "b.py": 150.0, "c.py": 120.0}
    # a.py unchanged mtime -> not in delta; b.py advanced; c.py new.
    assert activity.compute_delta(prev, cur) == {"b.py", "c.py"}


def test_compute_delta_empty_when_nothing_moved():
    snap = {"a.py": 100.0}
    assert activity.compute_delta(snap, snap) == set()


def test_attribute_single_namer_wins():
    delta = {"src/vupai/router.py", "lib/util.py"}
    tokens = {
        "%1": {"src/vupai/router.py"},
        "%2": {"README.md"},
    }
    assert activity.attribute(delta, tokens) == {
        "src/vupai/router.py": ["%1"],
        "lib/util.py": [],
    }


def test_attribute_two_namers_is_contended():
    delta = {"src/app.py"}
    tokens = {"%1": {"src/app.py"}, "%2": {"app.py"}}  # %2 names by basename
    assert activity.attribute(delta, tokens) == {"src/app.py": ["%1", "%2"]}


def test_attribute_basename_only_matches_changed_path():
    # A bare filename in scrollback matches a delta path by basename.
    assert activity.attribute({"a/b/c.py"}, {"%1": {"c.py"}}) == {
        "a/b/c.py": ["%1"]}


def test_classify_coverage_levels():
    f = activity.classify_coverage
    assert f(in_repo=True, attributed=True, marker=True, working=True) == "exact"
    assert f(in_repo=True, attributed=True, marker=False, working=True) == "git-delta"
    assert f(in_repo=True, attributed=False, marker=False, working=True) == "churn-only"
    assert f(in_repo=True, attributed=False, marker=False, working=False) == "none"
    assert f(in_repo=False, attributed=False, marker=False, working=True) == "none"


def test_store_append_and_read_history(tmp_path):
    store = activity.ActivityStore(tmp_path)
    store.append({"pane": "echo", "files": ["a.py"]})
    store.append({"pane": "orion", "files": ["b.py"]})
    assert [r["pane"] for r in store.read_history()] == ["echo", "orion"]


def test_store_seeds_gitignore_once(tmp_path):
    store = activity.ActivityStore(tmp_path)
    store.append({"x": 1})
    gi = tmp_path / ".vupai" / ".gitignore"
    assert gi.read_text(encoding="utf-8") == "*\n"


def test_store_current_is_atomic_roundtrip(tmp_path):
    store = activity.ActivityStore(tmp_path)
    snap = {"echo": {"pane": "echo", "files": ["a.py"]}}
    store.write_current(snap)
    assert store.read_current() == snap
    # No leftover temp file.
    assert not list((tmp_path / ".vupai").glob("*.tmp"))


def test_store_ring_bounds_history(tmp_path):
    store = activity.ActivityStore(tmp_path, history_limit=3)
    for i in range(5):
        store.append({"n": i})
    kept = [r["n"] for r in store.read_history()]
    assert kept == [2, 3, 4]


def test_store_read_current_missing_returns_empty(tmp_path):
    assert activity.ActivityStore(tmp_path).read_current() == {}


def test_git_toplevel_returns_stripped_root():
    calls = []

    def fake_git(tree, args):
        calls.append((tree, args))
        return "/Users/me/repo\n"

    assert activity.git_toplevel("/Users/me/repo/sub", fake_git) == "/Users/me/repo"
    assert calls == [("/Users/me/repo/sub", ["rev-parse", "--show-toplevel"])]


def test_git_toplevel_none_when_not_a_repo():
    assert activity.git_toplevel("/tmp/x", lambda tree, args: None) is None


def test_run_git_swallows_missing_binary(monkeypatch):
    def boom(*a, **k):
        raise OSError("git not found")
    monkeypatch.setattr(activity.subprocess, "run", boom)
    assert activity._run_git("/tmp", ["status"]) is None



# tmuxio.PANE_FORMAT order: id, window_id, window, index, name, command, active, session
def _pane_line(pid, name, *, session="proj", command="claude"):
    return "\t".join([pid, "@1", "win", "0", name, command, "1", session])


def _poller(panes_lines, *, captures, dirty, store):
    """panes_lines: PANE_FORMAT rows. captures: pid -> tail. dirty: tree ->
    porcelain text. store: a single ActivityStore captured by store_factory."""
    reg = PaneRegistry(lister=lambda: panes_lines, focuser=lambda: None)

    def fake_git(tree, args):
        if args[:1] == ["rev-parse"]:
            return "/tree\n"
        if args[:1] == ["status"]:
            return dirty.get(tree, "")
        return None

    mtimes = {"t": 0.0}

    def fake_stat(path):
        mtimes["t"] += 1.0  # every dirty file looks freshly touched each call
        return mtimes["t"]

    return activity.ActivityPoller(
        reg,
        capture_fn=lambda pid: captures.get(pid, ""),
        cwd_fn=lambda pid: "/tree",
        git_fn=fake_git,
        stat_fn=fake_stat,
        clock=lambda: "14:00",
        store_factory=lambda root: store,
    )


def test_tick_attributes_changed_file_to_naming_pane(tmp_path):
    store = activity.ActivityStore(tmp_path)
    p = _poller(
        [_pane_line("%1", "echo")],
        captures={"%1": "Update(src/router.py) done"},
        dirty={"/tree": " M src/router.py\n"},
        store=store,
    )
    p.tick()  # first tick: baseline snapshot, file appears in delta
    current = store.read_current()
    assert current["echo"]["files"] == ["src/router.py"]
    assert current["echo"]["coverage"] == "exact"  # Update(...) marker present


def test_tick_flags_contention_between_two_panes(tmp_path):
    store = activity.ActivityStore(tmp_path)
    p = _poller(
        [_pane_line("%1", "echo"), _pane_line("%2", "orion", command="codex")],
        captures={"%1": "editing src/app.py", "%2": "writing src/app.py now"},
        dirty={"/tree": " M src/app.py\n"},
        store=store,
    )
    p.tick()
    current = store.read_current()
    assert current["echo"]["contended_with"] == ["orion"]
    assert current["orion"]["contended_with"] == ["echo"]
    assert current["orion"]["coverage"] == "git-delta"  # codex, no marker


def test_tick_suppresses_bulk_change(tmp_path):
    store = activity.ActivityStore(tmp_path)
    big = "".join(f" M f{i}.py\n" for i in range(60))
    p = _poller(
        [_pane_line("%1", "echo")],
        captures={"%1": "f0.py"},
        dirty={"/tree": big},
        store=store,
    )
    p.tick()
    assert store.read_current() == {}  # 60 > bulk_threshold (50): no attribution


def test_tick_swallows_registry_failure():
    reg = PaneRegistry(lister=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                       focuser=lambda: None)
    p = activity.ActivityPoller(reg, store_factory=lambda root: None)
    p.tick()  # must not raise


def test_tick_emits_churn_only_for_active_unattributed_pane(tmp_path):
    # ember is active (its tail keeps changing) but names no path -> surfaced as
    # "active, files unknown" rather than silently omitted.
    store = activity.ActivityStore(tmp_path)
    captures = {"%1": "editing foo.py", "%2": "thinking"}
    dirty = {"/tree": " M foo.py\n"}
    p = _poller(
        [_pane_line("%1", "echo"), _pane_line("%2", "ember", command="opencode")],
        captures=captures, dirty=dirty, store=store)
    p.tick()  # baseline: ember has no prior tail, so no churn signal yet
    assert "ember" not in store.read_current()
    captures["%2"] = "thinking lots of brand new different output now here"
    p.tick()  # ember tail churned -> churn-only
    cur = store.read_current()
    assert cur["ember"]["coverage"] == "churn-only"
    assert cur["ember"]["files"] == []
    assert cur["ember"]["contended_with"] == []
    assert cur["echo"]["files"] == ["foo.py"]  # attribution still works alongside


def test_tick_idle_unattributed_pane_is_not_recorded(tmp_path):
    store = activity.ActivityStore(tmp_path)
    captures = {"%1": "editing foo.py", "%2": "$ "}
    dirty = {"/tree": " M foo.py\n"}
    p = _poller(
        [_pane_line("%1", "echo"), _pane_line("%2", "ember", command="opencode")],
        captures=captures, dirty=dirty, store=store)
    p.tick()
    p.tick()  # ember tail unchanged -> no churn, no marker -> never recorded
    assert "ember" not in store.read_current()


def test_tick_churn_only_via_working_marker_on_first_tick(tmp_path):
    # A Claude pane shows the working marker ("esc to interrupt") but no path:
    # active immediately, even with no prior tail to churn against.
    store = activity.ActivityStore(tmp_path)
    p = _poller(
        [_pane_line("%1", "echo"), _pane_line("%2", "nova", command="claude")],
        captures={"%1": "editing foo.py", "%2": "Esc to interrupt"},
        dirty={"/tree": " M foo.py\n"}, store=store)
    p.tick()
    assert store.read_current()["nova"]["coverage"] == "churn-only"


def test_collect_activity_reads_current_for_session(tmp_path):
    store = activity.ActivityStore(tmp_path)
    store.write_current({"echo": {
        "pane": "echo", "session": "proj", "tree": str(tmp_path),
        "files": ["a.py"], "coverage": "git-delta", "contended_with": []}})
    reg = PaneRegistry(
        lister=lambda: [_pane_line("%1", "echo", session="proj")],
        focuser=lambda: None)
    records = activity.collect_activity(
        reg, session="proj",
        cwd_fn=lambda pid: str(tmp_path),
        git_fn=lambda tree, args: str(tmp_path) + "\n")
    assert records == [store.read_current()["echo"]]


def test_collect_activity_omits_closed_panes(tmp_path):
    # "ghost" lingers in the ledger but is no longer a live pane: it must not
    # appear in the current activity view.
    store = activity.ActivityStore(tmp_path)
    store.write_current({
        "echo": {"pane": "echo", "session": "proj", "tree": str(tmp_path),
                 "files": ["a.py"], "coverage": "git-delta",
                 "contended_with": []},
        "ghost": {"pane": "ghost", "session": "proj", "tree": str(tmp_path),
                  "files": ["b.py"], "coverage": "git-delta",
                  "contended_with": []},
    })
    reg = PaneRegistry(
        lister=lambda: [_pane_line("%1", "echo", session="proj")],
        focuser=lambda: None)
    records = activity.collect_activity(
        reg, session="proj",
        cwd_fn=lambda pid: str(tmp_path),
        git_fn=lambda tree, args: str(tmp_path) + "\n")
    assert [r["pane"] for r in records] == ["echo"]


def test_tick_prunes_closed_pane_from_current(tmp_path):
    # current.json carries a stale "ghost" record; once a live pane edits, the
    # poller rewrites the snapshot without the vanished pane.
    store = activity.ActivityStore(tmp_path)
    store.write_current({
        "ghost": {"pane": "ghost", "session": "proj", "tree": "/tree",
                  "files": ["old.py"], "coverage": "git-delta",
                  "contended_with": []}})
    p = _poller(
        [_pane_line("%1", "echo")],
        captures={"%1": "Update(src/router.py) done"},
        dirty={"/tree": " M src/router.py\n"},
        store=store)
    p.tick()
    cur = store.read_current()
    assert "ghost" not in cur          # vanished pane pruned
    assert "echo" in cur               # live editor recorded


def test_extract_marked_paths_pulls_edited_file_from_markers():
    tail = "Update(src/router.py) then Updated lib/util.py with 3 additions"
    assert activity.extract_marked_paths(tail) == {"src/router.py", "lib/util.py"}


def test_extract_marked_paths_ignores_bare_mentions():
    assert activity.extract_marked_paths("we read README.md earlier") == set()


def test_tick_marker_owner_excludes_mention_only_pane(tmp_path):
    # astra PROVES it edited README.md (the Update() marker names the path);
    # nova merely mentions README.md in its scrollback. The change must be
    # attributed to astra alone, with no phantom cross-pane conflict.
    store = activity.ActivityStore(tmp_path)
    p = _poller(
        [_pane_line("%1", "astra"), _pane_line("%2", "nova")],
        captures={"%1": "Update(README.md) done",
                  "%2": "earlier we edited README.md then moved on"},
        dirty={"/tree": " M README.md\n"},
        store=store)
    p.tick()
    cur = store.read_current()
    assert cur["astra"]["files"] == ["README.md"]
    assert cur["astra"]["coverage"] == "exact"
    assert cur["astra"]["contended_with"] == []          # no phantom conflict
    # nova never claims README.md (it may still surface as churn-only active).
    assert "README.md" not in cur.get("nova", {}).get("files", [])


def test_tick_no_marker_still_flags_real_contention(tmp_path):
    # When NO pane proves the edit (markerless tools), fall back to mention-based
    # attribution so a genuine same-file conflict is still surfaced.
    store = activity.ActivityStore(tmp_path)
    p = _poller(
        [_pane_line("%1", "echo", command="codex"),
         _pane_line("%2", "orion", command="codex")],
        captures={"%1": "editing src/app.py", "%2": "writing src/app.py now"},
        dirty={"/tree": " M src/app.py\n"},
        store=store)
    p.tick()
    cur = store.read_current()
    assert cur["echo"]["contended_with"] == ["orion"]
    assert cur["orion"]["contended_with"] == ["echo"]
    assert cur["orion"]["coverage"] == "git-delta"


def test_render_activity_coverage_aware():
    out = activity.render_activity([
        {"pane": "echo", "files": ["router.py"], "coverage": "git-delta",
         "contended_with": ["orion"]},
        {"pane": "ember", "files": [], "coverage": "churn-only",
         "contended_with": []},
    ])
    assert "echo editing router.py" in out
    assert "also: orion" in out
    assert "ember active, files unknown" in out


def test_render_activity_empty():
    assert activity.render_activity([]) == "No recent activity."


def test_summarize_history_counts():
    recs = [
        {"coverage": "git-delta", "contended_with": ["x"]},
        {"coverage": "git-delta", "contended_with": []},
        {"coverage": "churn-only", "contended_with": []},
        {"coverage": "none", "contended_with": []},
    ]
    s = activity.summarize_history(recs)
    assert s["events"] == 4
    assert s["contention_rate"] == 0.25
    assert s["attributed_rate"] == 0.5  # 2 of 4 are exact/git-delta
    assert s["coverage_counts"]["git-delta"] == 2
