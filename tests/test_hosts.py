from pathlib import Path

from vupai.hosts import Host, load_hosts, resolve_host, slugify_host


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "hosts.toml"
    p.write_text(body)
    return p


def test_slugify_host_normalizes():
    assert slugify_host("GPU Box") == "gpu-box"
    assert slugify_host("vm.1") == "vm-1"
    assert slugify_host("  ") == ""


def test_load_hosts_missing_file_is_empty(tmp_path):
    assert load_hosts(tmp_path / "nope.toml") == {}


def test_load_hosts_parses_all_fields(tmp_path):
    path = _write(tmp_path, """
[hosts.vm1]
user = "jose"
host = "10.0.0.5"
program = "codex"

[hosts.gpubox]
host = "gpu.example.com"
port = 2222

[hosts.staging]
user = "ubuntu"
host = "staging.example.com"
""")
    hosts = load_hosts(path)
    assert hosts["vm1"] == Host(
        name="vm1", host="10.0.0.5", user="jose", port=None, program="codex"
    )
    assert hosts["gpubox"] == Host(
        name="gpubox", host="gpu.example.com", user=None, port=2222, program=None
    )
    assert hosts["staging"] == Host(
        name="staging", host="staging.example.com", user="ubuntu", program=None
    )


def test_load_hosts_skips_entry_without_host(tmp_path):
    path = _write(tmp_path, """
[hosts.broken]
user = "jose"

[hosts.ok]
host = "1.2.3.4"
""")
    hosts = load_hosts(path)
    assert "broken" not in hosts
    assert "ok" in hosts


def test_load_hosts_empty_program_string_preserved(tmp_path):
    # "" means an explicit plain remote shell; distinct from absent (None).
    path = _write(tmp_path, """
[hosts.shellbox]
host = "1.2.3.4"
program = ""
""")
    assert load_hosts(path)["shellbox"].program == ""


def test_load_hosts_malformed_toml_returns_empty(tmp_path):
    # Users hand-edit this file; a syntax error must degrade gracefully,
    # not crash daemon startup. Unclosed table header is invalid TOML.
    path = _write(tmp_path, '[hosts.vm1\nhost = "x"\n')
    assert load_hosts(path) == {}


def test_load_hosts_unreadable_path_returns_empty(tmp_path):
    # An existing-but-unreadable path (here a directory) makes .exists() true
    # but open() raises OSError; must degrade gracefully, not crash startup.
    p = tmp_path / "hosts.toml"
    p.mkdir()
    assert load_hosts(p) == {}


_HOSTS = {
    "vm1": Host(name="vm1", host="10.0.0.5"),
    "gpubox": Host(name="gpubox", host="gpu.example.com"),
}


def test_resolve_host_exact():
    m = resolve_host("vm1", _HOSTS)
    assert m.host is not None and m.host.name == "vm1"
    assert m.candidates == ()


def test_resolve_host_fuzzy_recovers_spacing():
    # "GPU Box" slugifies to "gpu-box"; fuzzy ratio to "gpubox" is ~92, above
    # the default cutoff, so a spoken-with-spaces host name still resolves.
    assert resolve_host("GPU Box", _HOSTS).host.name == "gpubox"


def test_resolve_host_below_cutoff_returns_none():
    # "vm one" -> "vm-one" scores only ~44 against "vm1"; below cutoff -> no match.
    assert resolve_host("vm one", _HOSTS).host is None


def test_resolve_host_miss_returns_none():
    assert resolve_host("database", _HOSTS).host is None


def test_resolve_host_empty_inventory():
    assert resolve_host("vm1", {}).host is None


def test_resolve_host_custom_cutoff_allows_loose_match():
    # A low cutoff lets a weaker fuzzy match through that the default would reject.
    # "vm one" -> "vm-one" scores ~44 vs "vm1": rejected at default 82, accepted at 40.
    assert resolve_host("vm one", _HOSTS).host is None
    assert resolve_host("vm one", _HOSTS, cutoff=40).host.name == "vm1"


def test_resolve_host_near_tie_is_ambiguous():
    # Two similarly-spelled keys both score within the margin of a non-exact
    # phrase: refuse to guess, return both as candidates (host stays None).
    hosts = {
        "data-api": Host(name="data-api", host="1"),
        "data-app": Host(name="data-app", host="2"),
    }
    m = resolve_host("data apt", hosts)
    assert m.host is None
    assert set(m.candidates) == {"data-api", "data-app"}


def test_resolve_host_exact_wins_over_near_tie():
    # An exact slug short-circuits before fuzzy, so a shared prefix never makes
    # an exactly-spoken name ambiguous.
    hosts = {
        "home": Host(name="home", host="1"),
        "home-playground": Host(name="home-playground", host="2"),
    }
    m = resolve_host("home", hosts)
    assert m.host is not None and m.host.name == "home"
    assert m.candidates == ()
