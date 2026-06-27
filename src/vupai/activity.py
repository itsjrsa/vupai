"""Cross-pane activity ledger (Layer 1).

Pull-only, best-effort awareness of which sibling pane touched which file.
Runs on its OWN thread with its OWN PaneRegistry; touches only tmux capture-pane
plus read-only git and its own .vupai/ store. It NEVER touches the recorder,
ASR/MLX, the injector, or the daemon's jobs queue (the worker isolation
contract, see watcher.py). All git/filesystem failures are swallowed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from . import tmuxio

logger = logging.getLogger(__name__)

# A path-shaped token: optional dirs, then a name, then an ALPHA-led extension.
# The alpha-led extension rejects version strings (v2.1.193, 0.142.2) and bare
# numbers, which a churning agent prints constantly. Attribution only, never
# the source of truth for "what changed" (git owns that).
_PATH_RE = re.compile(r"[A-Za-z0-9_./\-]*[A-Za-z0-9_\-]\.[A-Za-z][A-Za-z0-9]*")


def extract_paths(tail: str) -> set[str]:
    """Path-shaped tokens from a scrollback tail, normalized (leading ./ and
    / stripped). Tool-agnostic: matches what any TUI prints on screen."""
    return {m.group(0).lstrip("./") for m in _PATH_RE.finditer(tail)}


def parse_porcelain(text: str) -> set[str]:
    """Repo-relative paths from `git status --porcelain`. Each line is
    `XY <path>` (XY = 2 status chars + space); a rename is `XY old -> new`,
    of which we keep `new`."""
    paths: set[str] = set()
    for line in text.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.add(entry.strip().strip('"'))
    return paths


def compute_delta(prev: dict[str, float], cur: dict[str, float]) -> set[str]:
    """Paths newly dirty or whose mtime advanced since the previous tick."""
    return {p for p, mtime in cur.items() if p not in prev or mtime > prev[p]}


def _path_matches(delta_path: str, token: str) -> bool:
    """Whether a scrollback `token` names the changed `delta_path`. Matches on
    a path-component boundary, with a basename fallback for bare filenames."""
    d = delta_path.lstrip("./")
    t = token.lstrip("./")
    if d == t or d.endswith("/" + t) or t.endswith("/" + d):
        return True
    return "/" not in t and d.rsplit("/", 1)[-1] == t


def attribute(delta: set[str], pane_tokens: dict[str, set[str]]) -> dict[str, list[str]]:
    """For each changed path, the sorted pane ids whose scrollback names it.
    Empty list = unattributed (changed, pane unknown)."""
    result: dict[str, list[str]] = {}
    for path in delta:
        result[path] = sorted(
            pid for pid, toks in pane_tokens.items()
            if any(_path_matches(path, t) for t in toks)
        )
    return result


def classify_coverage(*, in_repo: bool, attributed: bool, marker: bool,
                      working: bool) -> str:
    """Honest coverage flag so "no entry" is never confused with "no conflict".
    exact: git delta intersected scrollback AND a tool edit-marker confirmed it.
    git-delta: intersected scrollback, no marker. churn-only: pane is active but
    no attributable file. none: not a git repo, or git failed / pane quiet."""
    if not in_repo:
        return "none"
    if attributed:
        return "exact" if marker else "git-delta"
    return "churn-only" if working else "none"


@dataclass
class ActivityStore:
    """Per-working-tree ledger files under <root>/<dir_name>/. Single writer
    (the poller); readers only read. Best-effort: IO failures are swallowed."""

    root: Path
    dir_name: str = ".vupai"
    history_limit: int = 500

    @property
    def _dir(self) -> Path:
        return self.root / self.dir_name

    @property
    def _jsonl(self) -> Path:
        return self._dir / "activity.jsonl"

    @property
    def _current(self) -> Path:
        return self._dir / "activity.current.json"

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        gitignore = self._dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")

    def append(self, entry: dict) -> None:
        try:
            self._ensure_dir()
            with self._jsonl.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._ring_bound()
        except OSError:
            logger.warning("activity: append failed for %s", self.root)

    def _ring_bound(self) -> None:
        try:
            lines = self._jsonl.read_text(encoding="utf-8").splitlines()
            if len(lines) <= self.history_limit:
                return
            kept = lines[-self.history_limit:]
            tmp = self._jsonl.with_name("activity.jsonl.tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            os.replace(tmp, self._jsonl)
        except OSError:
            pass

    def write_current(self, snapshot: dict) -> None:
        try:
            self._ensure_dir()
            tmp = self._current.with_name("activity.current.json.tmp")
            tmp.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            os.replace(tmp, self._current)
        except OSError:
            logger.warning("activity: current write failed for %s", self.root)

    def read_current(self) -> dict:
        try:
            return json.loads(self._current.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def read_history(self) -> list[dict]:
        records: list[dict] = []
        try:
            text = self._jsonl.read_text(encoding="utf-8")
        except OSError:
            return records
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records


def _run_git(tree: str, args: list[str], *, timeout: float = 2.0) -> str | None:
    """Run `git -C <tree> <args>` read-only with an explicit timeout. Returns
    stdout, or None on nonzero exit / OSError / timeout (all swallowed)."""
    try:
        proc = subprocess.run(
            ["git", "-C", tree, *args],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_toplevel(path: str, git_fn=_run_git) -> str | None:
    """Absolute git tree root containing `path`, or None if it is not a repo."""
    out = git_fn(path, ["rev-parse", "--show-toplevel"])
    if not out:
        return None
    return out.strip() or None


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


# Optional booster: an explicit tool edit-marker upgrades coverage to "exact".
# Claude prints Update(<path>) / "Updated <path> with N additions"; other tools
# have none and degrade to "git-delta". Tiebreak only, never required.
_EDIT_MARKER_RE = re.compile(r"\bUpdate\(|\bUpdated .+ with \d+ (addition|removal)")


def _has_edit_marker(tail: str) -> bool:
    return bool(_EDIT_MARKER_RE.search(tail))


def _excluded(path: str, excludes: tuple[str, ...]) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch(path, pat) or fnmatch(base, pat) for pat in excludes)


class ActivityPoller:
    """Background poller. Clones board.Board's thread skeleton. Owns its own
    PaneRegistry; touches only tmux capture-pane + read-only git + its store."""

    def __init__(self, registry, *, capture_fn=tmuxio.capture_pane,
                 cwd_fn=tmuxio.pane_current_path, git_fn=_run_git,
                 stat_fn=_mtime, clock=lambda: time.strftime("%H:%M"),
                 now=time.monotonic, poll_interval: float = 2.0,
                 recency_window_s: float = 30.0, history_limit: int = 500,
                 excludes: tuple[str, ...] = (), dir_name: str = ".vupai",
                 bulk_threshold: int = 50, store_factory=None) -> None:
        self._registry = registry
        self._capture = capture_fn
        self._cwd = cwd_fn
        self._git = git_fn
        self._stat = stat_fn
        self._clock = clock
        # _now and _recency are reserved for the deferred churn-only / recency
        # phase; the MVP poller is edge-driven by the git delta and does not
        # read them yet.
        self._now = now
        self._poll_interval = poll_interval
        self._recency = recency_window_s
        self._history_limit = history_limit
        self._excludes = excludes
        self._dir_name = dir_name
        self._bulk = bulk_threshold
        self._store_factory = store_factory or (
            lambda root: ActivityStore(
                Path(root), dir_name=dir_name, history_limit=history_limit))
        self._snapshots: dict[str, dict[str, float]] = {}  # tree -> {path: mtime}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- thread skeleton (clone of board.Board) ---
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.debug("activity tick failed", exc_info=True)
            if self._stop.wait(self._poll_interval):
                break

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    # --- work ---
    def tick(self) -> None:
        """One synchronous poll cycle. Safe to call directly in tests."""
        try:
            self._registry.refresh()
        except Exception:
            logger.debug("activity registry refresh failed", exc_info=True)
            return
        trees: dict[str, list] = {}
        toplevel: dict[str, str | None] = {}
        for pane in self._registry.panes:
            try:
                cwd = self._cwd(pane.id)
                if not cwd:
                    continue
                if cwd not in toplevel:
                    toplevel[cwd] = git_toplevel(cwd, self._git)
                root = toplevel[cwd]
                if root is None:
                    continue
                trees.setdefault(root, []).append(pane)
            except Exception:
                continue
        for root, panes in trees.items():
            try:
                self._process_tree(root, panes)
            except Exception:
                logger.debug("activity tree failed: %s", root, exc_info=True)

    def _dirty_mtimes(self, root: str) -> dict[str, float]:
        out = self._git(root, ["status", "--porcelain"])
        if out is None:
            return {}
        mtimes: dict[str, float] = {}
        for path in parse_porcelain(out):
            mtime = self._stat(os.path.join(root, path))
            if mtime is not None:
                mtimes[path] = mtime
        return mtimes

    def _process_tree(self, root: str, panes: list) -> None:
        cur = self._dirty_mtimes(root)
        prev = self._snapshots.get(root, {})
        delta = compute_delta(prev, cur)
        self._snapshots[root] = cur
        if not delta or len(delta) > self._bulk:
            return  # nothing changed, or a bulk rebase/checkout/test churn
        delta = {p for p in delta if not _excluded(p, self._excludes)}
        if not delta:
            return
        pane_tokens: dict[str, set[str]] = {}
        markers: dict[str, bool] = {}
        for pane in panes:
            try:
                tail = self._capture(pane.id)
            except Exception:
                tail = ""
            pane_tokens[pane.id] = extract_paths(tail)
            markers[pane.id] = _has_edit_marker(tail)
        attribution = attribute(delta, pane_tokens)
        by_pane: dict[str, list[str]] = {}
        contended: set[str] = set()
        for path, pids in attribution.items():
            if len(pids) > 1:
                contended.add(path)
            for pid in pids:
                by_pane.setdefault(pid, []).append(path)
        if not by_pane:
            return
        store = self._store_factory(root)
        snapshot = store.read_current()
        by_id = {p.id: p for p in panes}
        ts = self._clock()
        for pid, files in by_pane.items():
            pane = by_id.get(pid)
            if pane is None:
                continue
            files = sorted(files)
            others = sorted({
                by_id[op].name for op in by_pane
                if op != pid and op in by_id
                and set(by_pane[op]) & set(files) & contended
            })
            coverage = classify_coverage(
                in_repo=True, attributed=True, marker=markers.get(pid, False),
                working=True)
            record = {
                "ts": ts, "session": pane.session, "tree": root,
                "pane": pane.name, "pane_id": pid, "files": files,
                "coverage": coverage, "state": "working",
                "contended_with": others,
            }
            store.append(record)
            snapshot[pane.name] = record
        store.write_current(snapshot)


def _session_tree_roots(registry, *, session, cwd_fn, git_fn) -> list[str]:
    registry.refresh()
    roots: dict[str, None] = {}
    for pane in registry.panes:
        if session and pane.session != session:
            continue
        cwd = cwd_fn(pane.id)
        if not cwd:
            continue
        root = git_toplevel(cwd, git_fn)
        if root:
            roots[root] = None
    return list(roots)


def collect_activity(registry, *, session=None,
                     cwd_fn=tmuxio.pane_current_path, git_fn=_run_git,
                     dir_name: str = ".vupai") -> list[dict]:
    """Latest per-pane record from each tree's current.json, for the session."""
    records: list[dict] = []
    for root in _session_tree_roots(
            registry, session=session, cwd_fn=cwd_fn, git_fn=git_fn):
        store = ActivityStore(Path(root), dir_name=dir_name)
        for rec in store.read_current().values():
            if session and rec.get("session") not in (None, session):
                continue
            records.append(rec)
    return records


def collect_history(registry, *, session=None,
                    cwd_fn=tmuxio.pane_current_path, git_fn=_run_git,
                    dir_name: str = ".vupai") -> list[dict]:
    """All history events from each tree's activity.jsonl, for the session."""
    records: list[dict] = []
    for root in _session_tree_roots(
            registry, session=session, cwd_fn=cwd_fn, git_fn=git_fn):
        store = ActivityStore(Path(root), dir_name=dir_name)
        for rec in store.read_history():
            if session and rec.get("session") not in (None, session):
                continue
            records.append(rec)
    return records


def render_activity(records: list[dict]) -> str:
    """Coverage-aware one-line digest for speech / status line."""
    if not records:
        return "No recent activity."
    n = len(records)
    parts = [f"{n} pane{'s' if n != 1 else ''} active."]
    for rec in sorted(records, key=lambda r: r.get("pane", "")):
        files = rec.get("files") or []
        coverage = rec.get("coverage")
        if coverage in ("exact", "git-delta") and files:
            head = f"{rec['pane']} editing {', '.join(files[:3])}"
        elif coverage == "churn-only":
            head = f"{rec['pane']} active, files unknown"
        else:
            head = f"{rec['pane']}: {coverage or 'unknown'}"
        if rec.get("contended_with"):
            head += f" (also: {', '.join(rec['contended_with'])})"
        parts.append(head.rstrip(".") + ".")
    return " ".join(parts)


def summarize_history(records: list[dict]) -> dict:
    """Phase 0 gate counters: contention frequency + attribution accuracy.
    Used to decide whether Layer 2 (live diff review) is worth building."""
    total = len(records)
    contended = sum(1 for r in records if r.get("contended_with"))
    counts: dict[str, int] = {}
    for rec in records:
        cov = rec.get("coverage", "none")
        counts[cov] = counts.get(cov, 0) + 1
    attributed = counts.get("exact", 0) + counts.get("git-delta", 0)
    return {
        "events": total,
        "contention_rate": (contended / total) if total else 0.0,
        "attributed_rate": (attributed / total) if total else 0.0,
        "coverage_counts": counts,
    }
