"""testing.suite_runs -- the last two suite-run records per collection with a freshness verdict.

COMPUTE_ONLY: reads ``.coordinator-local/suite-runs`` and asks git what moved since each
recorded sha; runs no tests and writes nothing.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.testing import suite_run_record as srr
from coordinator_core.win_portability import no_console_creationflags

_GIT_TIMEOUT_S = 30
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _git(root: Path, *args: str) -> "list[str] | None":
    """Stdout lines of ``git <args>`` in ``root``, None on a non-zero exit."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_S, **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line]


def _worktree_paths(root: Path) -> "list[str] | None":
    """Tracked-dirty and untracked paths from one ``git status``; ``.coordinator-local`` and
    classifier-docs paths are excluded."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v2", "-z", "--untracked-files=normal", "--",
             ".", ":!.coordinator-local", *srr.DOCS_EXCLUDE_PATHSPECS],
            cwd=str(root), capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    paths: list = []
    entries = iter(proc.stdout.split("\0"))
    for entry in entries:
        if entry[:2] in ("? ",):
            paths.append(entry[2:])
        elif entry[:2] in ("1 ", "u "):
            paths.append(entry.split(" ", 8 if entry[0] == "1" else 10)[-1])
        elif entry[:2] == "2 ":
            paths.append(entry.split(" ", 9)[-1])
            paths.append(next(entries, ""))
    return [p for p in paths if p]


def _committed_since(root: Path, oldest: str, shas: "list[str]") -> "dict[str, set[str]]":
    """Files changed in ``<sha>..HEAD`` per sha met while walking ``<oldest>..HEAD`` (one
    ``git log``); ``oldest`` itself maps to the whole range. Others are absent."""
    try:
        proc = subprocess.run(
            ["git", "log", "--no-renames", "--name-only", "-z", "--format=%x01%H",
             f"{oldest}..HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    since: dict = {}
    acc: set = set()
    for chunk in proc.stdout.split("\x01")[1:]:
        head, *files = chunk.lstrip().split("\0")
        head = head.strip().lower()
        for sha in shas:
            if head.startswith(sha.lower()) and sha not in since:
                since[sha] = set(acc)
        acc.update(f.strip("\n") for f in files if f.strip("\n"))
    for sha in shas:
        if sha == oldest:
            since[sha] = acc
    return since


def _verdict(rec: dict, changed: "list[str] | None", untracked: "list[str]") -> dict:
    """Freshness of one record; ``changed`` is None when its sha did not resolve."""
    _docs, dirty_src = srr.classify_changed(rec.get("dirty_paths") or [])
    if dirty_src:
        return {"fresh": False, "reason": "dirty-at-run", "changed_files": dirty_src}
    if changed is None:
        return {"fresh": False, "reason": "sha-unresolvable"}
    docs_only, source = srr.classify_changed(sorted({*changed, *untracked}))
    if docs_only:
        return {"fresh": True}
    return {"fresh": False, "changed_files": source}


def _collections(base: Path, wanted: Optional[str]) -> "list[str]":
    if wanted:
        return [wanted]
    try:
        return sorted(p.name for p in base.iterdir() if p.is_dir())
    except OSError:
        return []


def _resolve_root(params: dict, repo_root: Optional[Path]) -> Optional[Path]:
    if params.get("repo_root"):
        return srr.repo_root_for(params["repo_root"])
    if repo_root:
        return main_worktree_root(repo_root)
    return srr.repo_root_for(Path.cwd())


@register_op("testing.suite_runs")
def _suite_runs(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: repo_root (optional), collection (optional; all when absent), failed_ids
    (optional list; default each collection's current record's). Returns
    ``{"repo_root", "collections": {name: {"current", "previous", "main"?, "failure_split"?}}}``;
    each non-None slot carries the record plus a ``freshness`` verdict (``main`` is the raw
    record, no verdict). ``failure_split`` is ``srr.classify_failures``: per id, ``pre-existing``
    (red at the previous banked sha or main, shas named) or ``new``; read from disk, no extra
    spawn. Git spawns: 0 with no records, else
    exactly 2 regardless of record or sha count (one ``status``, one ``git log <oldest>..HEAD``; the log is skipped when every sha is HEAD)."""
    root = _resolve_root(params, repo_root)
    if root is None:
        return {"repo_root": None, "collections": {}}
    base = root / ".coordinator-local" / "suite-runs"
    loaded: dict = {}
    mains: dict = {}
    raw_ids = params.get("failed_ids")
    wanted_ids = [str(i) for i in raw_ids] if isinstance(raw_ids, list) else None
    for name in _collections(base, params.get("collection")):
        try:
            rec_dir = srr.record_dir(root, name)
        except ValueError:
            continue
        runs = srr.read_runs(rec_dir)
        main = srr.read_main(rec_dir)
        if any(runs.values()) or main:
            loaded[name] = runs
            mains[name] = main

    started: dict = {}
    for runs in loaded.values():
        for rec in runs.values():
            if rec and rec.get("head_sha") and not srr.classify_changed(rec.get("dirty_paths") or [])[1]:
                sha, at = rec["head_sha"], str(rec.get("started_at") or "")
                started[sha] = min(at, started.get(sha, at))
    shas = sorted(started)
    changed_by_sha: dict = {}
    untracked: list = []
    if shas:
        work = _worktree_paths(root)
        untracked = work or []
        head = (srr.read_head_sha(root) or "").lower()
        at_head = [sha for sha in shas if head and head.startswith(sha.lower())]
        since = {sha: set() for sha in at_head}
        walk = [sha for sha in shas if sha not in at_head and _SHA_RE.match(sha)]
        if walk:
            oldest = min(walk, key=lambda sha: (started[sha], sha))
            since.update(_committed_since(root, oldest, walk))
        changed_by_sha = {
            sha: (sorted(since[sha]) if sha in since and work is not None else None)
            for sha in shas
        }

    out: dict = {}
    for name, runs in loaded.items():
        out[name] = {
            slot: (None if rec is None else {
                **rec,
                "freshness": _verdict(rec, changed_by_sha.get(rec.get("head_sha")), untracked),
            })
            for slot, rec in runs.items()
        }
        main = mains.get(name)
        if main is not None:
            out[name][srr.MAIN_SLOT] = main
        ids = wanted_ids if wanted_ids is not None else ((runs.get("current") or {}).get("failed_ids") or [])
        if ids:
            out[name]["failure_split"] = srr.classify_failures(ids, runs.get("previous"), main, runs.get("current"))
    return {"repo_root": str(root), "collections": out}
