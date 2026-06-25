from pathlib import Path

from vupai.hosts import Host, load_hosts, slugify_host


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
