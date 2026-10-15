"""Abandoned-run contract: ``dispatch.reap_abandoned_runs`` lands the ``state/warp/<run_id>/``
record dirs of ask runs that died before any terminal or halted commit.

Params: ``{}`` or ``{"dry_run": bool}``.
Reply: ``{ok, reaped: [run_id], sizings: [rel], committed_sha | null, skipped: [{run_id, reason}]}``.

A run is abandoned when it has changed top-level record files, no live or claimed run token, and
its newest record file is older than ``run_token.TTL_SECONDS``. The age floor covers a run whose
token mint failed. This module holds the pure contract; the handler is added beside it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, halted_record_message
from coordinator_core.ops.dispatch_emit.halted_record_commit import record_dir_changes

OP_NAME = "dispatch.reap_abandoned_runs"
HALTED_KIND = "abandoned"
SIZING_ROOT = "state/sizings"
RUN_FILE = "run.json"


def abandoned_run_ids(
    changed: set[str],
    live_run_ids: set[str],
    record_root: str,
    newest_mtime: dict[str, float],
    now: float,
) -> list[str]:
    """Sorted run ids with a changed file directly under ``<record_root>/<run_id>/``, absent from
    ``live_run_ids`` (live AND claimed tokens), whose newest mtime is older than the token TTL.
    A run id with no ``newest_mtime`` entry is not abandoned. Pure."""
    prefix = record_root.rstrip("/") + "/"
    candidates: set[str] = set()
    for rel in changed:
        if not rel.startswith(prefix):
            continue
        run_id, sep, leaf = rel[len(prefix):].partition("/")
        if run_id and sep and leaf and "/" not in leaf:
            candidates.add(run_id)
    return sorted(
        run_id
        for run_id in candidates - live_run_ids
        if run_id in newest_mtime and now - newest_mtime[run_id] > run_token.TTL_SECONDS
    )


def reap_message(run_ids: list[str]) -> str:
    """Commit message for the reaper's one commit: the halted-record form with
    ``Halted-By: abandoned``. Pure."""
    return halted_record_message(", ".join(run_ids), {"halted": HALTED_KIND})


def _token_records(repo_root: Path) -> list[dict]:
    """Every live record plus every claimed one (a run mid terminal-commit). One directory
    listing each; no spawn."""
    records = list(run_token.live_records(repo_root))
    common = run_token._common_dir(repo_root)
    if common is not None:
        try:
            claimed = sorted((common / run_token._DIR / run_token._CLAIMED).glob("*.json"))
        except OSError:
            claimed = []
        for path in claimed:
            record = run_token._read(path)
            if record is not None and isinstance(record.get("run_id"), str):
                records.append(record)
    return records


def _run_sizing(run_dir: Path) -> Optional[str]:
    """The sizing a run's ``run.json`` names, repo-relative, else ``None``."""
    try:
        data = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rel = data.get("sizing_rel") if isinstance(data, dict) else None
    return rel.replace("\\", "/") if isinstance(rel, str) and rel else None


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.reap_abandoned_runs": one status read, at most one commit.

    Lands every abandoned run's top-level record files plus the dirty sizing its ``run.json``
    names (unless a live or claimed run token names it) as one ``ceremony.commit_v2`` call."""
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops.dispatch_emit.terminal_commit import _worktree_snapshot
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    if repo_root is None:
        return {"error": f"{OP_NAME} requires repo_root"}
    params = params if isinstance(params, dict) else {}
    root = main_worktree_root(Path(repo_root))
    base: dict = {"ok": True, "reaped": [], "sizings": [], "committed_sha": None, "skipped": []}

    snapshot = _worktree_snapshot(root, [], [RECORD_DIR_ROOT, SIZING_ROOT], [])
    if snapshot is None:
        return {**base, "ok": False, "error": "git status over the record and sizing roots failed"}
    records = _token_records(root)
    live_ids = {r["run_id"] for r in records}
    held_sizings = {str(r.get("sizing_rel") or "").replace("\\", "/") for r in records} - {""}

    files: dict[str, list[str]] = {}
    deleted: list[str] = []
    newest: dict[str, float] = {}
    run_ids = {
        rel[len(RECORD_DIR_ROOT) + 1:].split("/", 1)[0]
        for rel in snapshot.changed
        if rel.startswith(RECORD_DIR_ROOT + "/")
    }
    for run_id in sorted(run_ids):
        present, gone = record_dir_changes(root, f"{RECORD_DIR_ROOT}/{run_id}", snapshot.changed)
        files[run_id] = present
        deleted += gone
        mtimes = []
        for rel in present:
            try:
                mtimes.append((root / rel).stat().st_mtime)
            except OSError:
                pass
        if mtimes:
            newest[run_id] = max(mtimes)

    reaped = abandoned_run_ids(snapshot.changed, live_ids, RECORD_DIR_ROOT, newest, time.time())
    base["skipped"] = [{"run_id": r, "reason": "live-or-recent"} for r in sorted(run_ids) if r not in reaped]
    if not reaped:
        return base

    paths: list[str] = []
    sizings: list[str] = []
    for run_id in reaped:
        paths += files[run_id]
        sizing = _run_sizing(root / RECORD_DIR_ROOT / run_id)
        if (
            sizing and sizing in snapshot.changed and sizing not in held_sizings
            and sizing not in sizings and (root / sizing).is_file()
        ):
            sizings.append(sizing)
    gone = [d for d in deleted if d.split("/")[len(RECORD_DIR_ROOT.split("/"))] in reaped]
    base["reaped"], base["sizings"] = reaped, sizings
    if params.get("dry_run") is True:
        return base

    commit_params: dict = {"paths": paths + sizings, "message": reap_message(reaped)}
    if gone:
        commit_params["deleted_paths"] = gone
        commit_params["declared_reverts"] = gone
    session_id = params.get("session_id")
    if isinstance(session_id, str) and _UUID_RE.fullmatch(session_id):
        commit_params["session_id"] = session_id
    try:
        reply = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=root)
    except OpUnavailableError:
        return {**base, "ok": False, "error": "ceremony.commit_v2 is not registered"}
    if not isinstance(reply, dict) or reply.get("error") or not reply.get("committed"):
        return {**base, "ok": False, "error": f"commit_v2 landed nothing: {reply!r}"}
    return {**base, "committed_sha": reply.get("sha")}
