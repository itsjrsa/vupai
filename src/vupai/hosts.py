"""Inventory of remote machines for the spoken `ssh <host>` command.

A separate file from config.toml: machine inventory is infra, not app
preference, so it stays easy to gitignore or share on its own. Loaded once at
daemon startup (vupai reload to pick up edits), mirroring config.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

HOSTS_PATH = Path.home() / ".config" / "vupai" / "hosts.toml"


@dataclass(frozen=True)
class Host:
    name: str
    host: str
    user: str | None = None
    port: int | None = None
    # None = use the global config.pane_command; "" = an explicit plain remote
    # shell (no agent); any other string = that remote program.
    program: str | None = None


def slugify_host(raw: str) -> str:
    """Normalize a host key/phrase the way session names are slugified."""
    return re.sub(r"[.\s:]+", "-", raw.strip().lower()).strip("-")


def load_hosts(path: Path | None = None) -> dict[str, Host]:
    """Parse hosts.toml into {slug: Host}. Missing file -> {}; an entry without a
    non-empty `host` is skipped (never raises)."""
    target = path if path is not None else HOSTS_PATH
    if not target.exists():
        return {}
    with target.open("rb") as fh:
        data = tomllib.load(fh)
    table = data.get("hosts")
    if not isinstance(table, dict):
        return {}
    out: dict[str, Host] = {}
    for key, entry in table.items():
        if not isinstance(entry, dict):
            continue
        host = entry.get("host")
        if not isinstance(host, str) or not host:
            continue
        slug = slugify_host(key)
        if not slug:
            continue
        user = entry.get("user")
        port = entry.get("port")
        program = entry.get("program")
        out[slug] = Host(
            name=slug,
            host=host,
            user=user if isinstance(user, str) and user else None,
            port=port if isinstance(port, int) else None,
            program=program if isinstance(program, str) else None,
        )
    return out
