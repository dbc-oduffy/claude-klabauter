"""Idle and workday-close atlas drift catch-up: drift that never passed through `commit_scoped`.

`catch_up` reads HEAD without a spawn, compares it with a watermark, and only when HEAD moved runs
one `git log --name-status` over the unfolded range, plans the repair with `atlas_freshness`,
records each finding as an acted fact and queues the mechanical `files:` deltas. It never commits,
never stages and never writes a tracked file; the queued deltas ride the next engine commit.

This module is the only reader and writer of the watermark and pending files, so `git_native`
never parses their format. Every read-modify-write runs under `_lock` and lands by
write-temp-then-`os.replace`, re-reading inside the lock so a concurrent writer is merged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.cartography import atlas_freshness
from coordinator_core.cartography.atlas_freshness import ActedFact
from coordinator_core.git.run import run_git

FRESHNESS_DIR_RELPATH = ".coordinator-local/freshness"
WATERMARK_RELPATH = f"{FRESHNESS_DIR_RELPATH}/atlas-watermark.json"
"""`{"sha": <last caught-up HEAD>, "folded": [<shas commit_scoped already folded>]}`."""
PENDING_RELPATH = f"{FRESHNESS_DIR_RELPATH}/atlas-pending.json"
"""`{"<page relpath>": <files delta>}`: repairs queued for the next engine commit."""

CATCHUP_INTERVAL_SECS = 600
"""Minimum seconds between idle-tick catch-ups of one repo."""
CATCHUP_RANGE_CAP = 200
"""Max commits per run; beyond it one `catch_up_capped` fact is recorded and the watermark advances."""
FOLDED_CAP = 256
"""Max shas kept in the watermark's `folded` list."""

_ATLAS_MARKER = "docs/architecture/file-index.md"
_INDEX_PAGE = _ATLAS_MARKER
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_log = logging.getLogger(__name__)
_lock = threading.Lock()
_next_tick: dict[Path, float] = {}


@dataclass(frozen=True)
class CatchUpResult:
    """Outcome of one `catch_up`: `ran` is True when a `git log` range was folded; `spawns` counts
    the subprocesses it started; `facts` the acted facts recorded; `pending` the pages queued."""

    ran: bool
    spawns: int
    facts: int
    pending: int
    reason: str


def _idle(reason: str, spawns: int = 0) -> CatchUpResult:
    return CatchUpResult(False, spawns, 0, 0, reason)


def _gitdirs(repo: Path) -> tuple[Path, Path] | None:
    """Return `(gitdir, commondir)`; a worktree's `.git` file points at its own gitdir."""
    dot_git = repo / ".git"
    try:
        if dot_git.is_file():
            text = dot_git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            gitdir = Path(text[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = repo / gitdir
        else:
            gitdir = dot_git
        common = gitdir
        marker = gitdir / "commondir"
        if marker.is_file():
            common = gitdir / marker.read_text(encoding="utf-8").strip()
        return gitdir, common
    except OSError:
        return None


def _packed_ref(common: Path, ref: str) -> str | None:
    try:
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == ref and _SHA_RE.match(parts[0]):
                return parts[0]
    except OSError:
        pass
    return None


def _read_head(repo: Path) -> str | None:
    """Resolve HEAD to a sha with file reads only; None when unborn or unreadable."""
    dirs = _gitdirs(repo)
    if dirs is None:
        return None
    gitdir, common = dirs
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if _SHA_RE.match(head):
        return head
    if not head.startswith("ref:"):
        return None
    ref = head[len("ref:"):].strip()
    for base in (gitdir, common):
        try:
            value = (base / ref).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _SHA_RE.match(value):
            return value
    return _packed_ref(common, ref)


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _read_watermark(repo: Path) -> tuple[str, list[str]]:
    raw = _read_json(repo / WATERMARK_RELPATH)
    if not isinstance(raw, dict):
        return "", []
    folded = raw.get("folded")
    sha = raw.get("sha")
    return (
        sha if isinstance(sha, str) else "",
        [s for s in folded if isinstance(s, str)] if isinstance(folded, list) else [],
    )


def _write_watermark(repo: Path, sha: str, folded: list[str]) -> None:
    _write_json(repo / WATERMARK_RELPATH, {"sha": sha, "folded": folded[-FOLDED_CAP:]})


def read_pending(repo: str | Path) -> dict[str, int]:
    """Return the queued `files:` delta per page relpath; an absent file is an empty dict."""
    raw = _read_json(Path(repo) / PENDING_RELPATH)
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, int) and v != 0}


def _git_log(repo: Path, wm: str) -> list[tuple[str, list[tuple[str, str]]]] | None:
    """One `git log` over `wm..HEAD`, newest first; None when git rejects the range."""
    proc = run_git(
        ["-c", "core.quotepath=off", "log", "--no-renames", "--name-status",
         "--format=%H", "-n", str(CATCHUP_RANGE_CAP), f"{wm}..HEAD"],
        cwd=str(repo),
    )
    if proc.returncode != 0:
        return None
    commits: list[tuple[str, list[tuple[str, str]]]] = []
    for line in proc.stdout.splitlines():
        if _SHA_RE.match(line):
            commits.append((line, []))
        elif "\t" in line and commits:
            status, _, path = line.partition("\t")
            commits[-1][1].append((status[:1], path.rsplit("\t", 1)[-1]))
    return commits


def _net_changes(commits: list[tuple[str, list[tuple[str, str]]]]) -> tuple[list[str], set[str], set[str]]:
    """Fold newest-first commits into `(paths, deleted, added)` net of the whole range: a path
    added then deleted is dropped, one deleted then re-added is a modification."""
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    for _, changes in reversed(commits):
        for status, path in changes:
            first.setdefault(path, status)
            last[path] = status
    paths: list[str] = []
    deleted: set[str] = set()
    added: set[str] = set()
    for path, op in last.items():
        existed, exists = first[path] != "A", op != "D"
        if not existed and not exists:
            continue
        paths.append(path)
        if existed and not exists:
            deleted.add(path)
        elif exists and not existed:
            added.add(path)
    return paths, deleted, added


def catch_up(repo: str | Path) -> CatchUpResult:
    """Fold the commits since the watermark into the acted ledger and the pending queue."""
    root = Path(repo)
    if not (root / _ATLAS_MARKER).is_file():
        return _idle("no-atlas")
    head = _read_head(root)
    if head is None:
        return _idle("no-head")
    wm, _ = _read_watermark(root)
    if wm == head:
        return _idle("up-to-date")
    if not wm:
        with _lock:
            if not _read_watermark(root)[0]:
                _write_watermark(root, head, [])
        return _idle("seeded")
    commits = _git_log(root, wm)
    if commits is None:
        with _lock:
            _write_watermark(root, head, _read_watermark(root)[1])
            atlas_freshness.record_acted(
                root, [ActedFact(_INDEX_PAGE, head, "catch_up_reseeded", f"watermark {wm[:12]} not in range")], head
            )
        return CatchUpResult(False, 1, 1, 0, "reseeded")
    with _lock:
        _, folded = _read_watermark(root)
        skip = set(folded)
        fresh = [c for c in commits if c[0] not in skip]
        paths, deleted, added = _net_changes(fresh)
        plans, facts = atlas_freshness.plan_repair(root, paths, deleted, added)
        if len(commits) >= CATCHUP_RANGE_CAP:
            facts.append(
                ActedFact(_INDEX_PAGE, "", "catch_up_capped", f"only the newest {CATCHUP_RANGE_CAP} commits folded")
            )
        pending = read_pending(root)
        for plan in plans:
            total = pending.get(plan.page_relpath, 0) + plan.files_delta
            if total:
                pending[plan.page_relpath] = total
            else:
                pending.pop(plan.page_relpath, None)
        queued = [p.page_relpath for p in plans if p.page_relpath in pending]
        facts += [
            ActedFact(page, "", "repair_pending", f"files_delta {pending[page]:+d} queued for the next commit")
            for page in queued
        ]
        if plans:
            _write_json(root / PENDING_RELPATH, pending)
        atlas_freshness.record_acted(root, facts, head)
        _write_watermark(root, head, folded)
    return CatchUpResult(True, 1, len(facts), len(pending), "folded")


def on_idle_tick(repos: Iterable[Path]) -> bool:
    """Run `catch_up` for each repo past its interval; True when any repo did work."""
    now = time.monotonic()
    worked = False
    seen: set[Path] = set()
    for raw in repos:
        try:
            repo = Path(raw).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if repo in seen or not repo.is_dir():
            continue
        seen.add(repo)
        if now < _next_tick.get(repo, float("-inf")):
            continue
        _next_tick[repo] = now + CATCHUP_INTERVAL_SECS
        try:
            worked = catch_up(repo).ran or worked
        except Exception as exc:  # noqa: BLE001 -- the tick never fails on a catch-up error
            _log.warning("atlas catch-up failed for %s: %s", repo, exc)
            try:
                atlas_freshness.record_acted(
                    repo, [ActedFact(_INDEX_PAGE, "", "fold_error", f"catch-up: {type(exc).__name__}")], "uncommitted"
                )
            except Exception:  # noqa: BLE001 -- the failure record is best-effort
                pass
    return worked


def settle_commit(repo: str | Path, applied_pages: Iterable[str], sha: str, parent_sha: str) -> None:
    """Drop `applied_pages` from the pending queue, append `sha` to `folded`, and advance the
    watermark to `sha` when it equals `parent_sha`."""
    root = Path(repo)
    if not (root / _ATLAS_MARKER).is_file():
        return
    applied = set(applied_pages)
    with _lock:
        pending = read_pending(root)
        if applied & pending.keys():
            _write_json(root / PENDING_RELPATH, {k: v for k, v in pending.items() if k not in applied})
        wm, folded = _read_watermark(root)
        if not wm:
            _write_watermark(root, sha, [sha])
            return
        if sha not in folded:
            folded.append(sha)
        _write_watermark(root, sha if wm == parent_sha else wm, folded)
