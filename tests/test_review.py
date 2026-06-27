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
