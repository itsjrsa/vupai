from vupai import review


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
