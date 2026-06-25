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

from rapidfuzz import fuzz

HOSTS_PATH = Path.home() / ".config" / "vupai" / "hosts.toml"


@dataclass(frozen=True)
class Host:
    name: str
    host: str
    user: str | None = None
    port: int | None = None
    # None/"" = no agent, just open a remote login shell (the default); any other
    # string = the program to start on the remote.
    program: str | None = None


@dataclass(frozen=True)
class HostMatch:
    """Result of resolving a spoken phrase to a host.

    `host` is set on a confident match; `candidates` is non-empty only on an
    ambiguous near-tie (and then `host` is None), mirroring router.NameMatch so
    the ssh command can say "ambiguous - say the name again" like pane routing.
    """
    host: Host | None
    candidates: tuple[str, ...] = ()


def slugify_host(raw: str) -> str:
    """Normalize a host key/phrase the way session names are slugified."""
    return re.sub(r"[.\s:]+", "-", raw.strip().lower()).strip("-")


def load_hosts(path: Path | None = None) -> dict[str, Host]:
    """Parse hosts.toml into {slug: Host}. Missing file -> {}; an entry without a
    non-empty `host` is skipped (never raises)."""
    target = path if path is not None else HOSTS_PATH
    if not target.exists():
        return {}
    try:
        with target.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        # Unreadable (bad perms, or a directory) or hand-edited with a syntax
        # error: degrade gracefully rather than crash daemon startup.
        return {}
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


def resolve_host(
    phrase: str, hosts: dict[str, Host], *, cutoff: int = 82, ambiguity_margin: int = 5
) -> HostMatch:
    """Resolve a spoken phrase to a host, with near-tie ambiguity detection.

    Exact slug match wins outright. Otherwise the best rapidfuzz ratio over the
    keys, if it meets `cutoff` - but when two or more keys land within
    `ambiguity_margin` of the top (all above the cutoff), refuse to guess and
    return them as `candidates` so the caller can ask the user to repeat (same
    contract as router.resolve_pane_by_name). HostMatch(None) on no match or
    empty inventory. Forgiving on purpose, like pane-name routing.
    """
    if not hosts:
        return HostMatch(None)
    slug = slugify_host(phrase)
    if not slug:
        return HostMatch(None)
    if slug in hosts:
        return HostMatch(hosts[slug])
    scored = sorted(
        ((key, fuzz.ratio(slug, key)) for key in hosts),
        key=lambda ks: ks[1],
        reverse=True,
    )
    above = [(key, score) for key, score in scored if score >= cutoff]
    if not above:
        return HostMatch(None)
    best_key, best_score = above[0]
    near = [key for key, score in above if best_score - score <= ambiguity_margin]
    if len(near) > 1:
        return HostMatch(None, tuple(near))
    return HostMatch(hosts[best_key])
