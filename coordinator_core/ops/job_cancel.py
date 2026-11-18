"""job.start / job.cancel: register a long job's process tree, lock and staging; stop it by record.

job.start writes `<settings home>/jobs/<job_id>.json` (root pid and creation time, lock path,
staging path). job.cancel reaps exactly the descendants of that (pid, ctime) root, then removes the
lock file and staging dir and deletes the record. Spawn-free by contract: nothing here imports
`subprocess`. Contract for peers: docs/reference/job-cancel-contract.md.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from coordinator_core.bash_guards import _host_probe, _process_kill
from coordinator_core.bash_guards._taskstop_kill import kill_tree_verified
from coordinator_core.ipc import register_op
from coordinator_core.jobs import job_record
from coordinator_core.jobs.job_record import JobRecord, JobRecordError
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params

_START_PARAMS = (
    Field("job_id", "nonempty_str", required=True),
    Field("root_pid", "pos_int", required=True),
    Field("lock_path", "str"),
    Field("staging_path", "str"),
    Field("owner_repo", "str"),
    Field("command", "str"),
)

_CANCEL_PARAMS = (
    Field("job_id", "nonempty_str", required=True),
    Field("reason", "str"),
)


class _Kill:
    """KillPrimitives over the verified (pid, ctime) terminate of `_process_kill`."""

    terminate_verified = staticmethod(_process_kill.terminate_verified)
    command_line = staticmethod(_process_kill.command_line)


def _ancestors(rows: Sequence, pid: int) -> set:
    """Pids on `pid`'s parent chain (pid itself included); a parent younger than its child is pid
    reuse and ends the walk."""
    by_pid = {r.pid: r for r in rows}
    chain = {pid}
    cur = by_pid.get(pid)
    while cur is not None:
        parent = by_pid.get(cur.ppid)
        if parent is None or parent.pid in chain or parent.ctime > cur.ctime:
            break
        chain.add(parent.pid)
        cur = parent
    return chain


def _descendants(rows: Sequence, root_pid: int, root_ctime: int) -> list:
    """Root plus every descendant, leaves first; a child older than its parent is pid reuse."""
    by_ppid: dict = {}
    for r in rows:
        by_ppid.setdefault(r.ppid, []).append(r)
    root = next((r for r in rows if r.pid == root_pid and r.ctime == root_ctime), None)
    if root is None:
        return []
    order = [root]
    seen = {(root.pid, root.ctime)}
    i = 0
    while i < len(order):
        parent = order[i]
        i += 1
        for child in by_ppid.get(parent.pid, ()):
            key = (child.pid, child.ctime)
            if key not in seen and child.ctime >= parent.ctime:
                seen.add(key)
                order.append(child)
    return order[::-1]


@register_op("job.start")
def _start(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "job.start": params `job_id` (safe id), `root_pid` (int), `lock_path` / `staging_path`
    (absolute path or absent), `owner_repo`, `command` (all optional str).

    Reads the root's creation time and writes the job record. Replies `{ok, job_id, root_pid,
    root_ctime, record}` or `{"error": str}` for a malformed request, an unsafe id, a dead root, a
    root that is this engine or one of its ancestors, or a live record under the same id.
    """
    refusal = validate_params("job.start", params, _START_PARAMS, strict=True)
    if refusal is not None:
        return refusal
    job_id = params["job_id"]
    root_pid = params["root_pid"]
    for key in ("lock_path", "staging_path"):
        value = params.get(key)
        if value is not None and not os.path.isabs(value):
            return {"error": f"job.start params.{key} must be an absolute path"}
    try:
        path = job_record.record_path(job_id)
        existing = job_record.read_record(job_id)
    except JobRecordError as exc:
        if "unsafe" in str(exc):
            return {"error": f"job.start: {exc}"}
        existing = None
    if existing is not None and _host_probe.alive(existing.root_pid, existing.root_ctime):
        return {"error": f"job.start: job {job_id} already has a live record (root pid {existing.root_pid})"}
    root_ctime = _host_probe.creation_time(root_pid)
    if root_ctime is None:
        return {"error": f"job.start: root_pid {root_pid} is not a running process"}
    rows = _host_probe.snapshot()
    if not rows:
        return {"error": "job.start: process table unreadable; cannot rule out the engine's own tree"}
    if root_pid in _ancestors(rows, os.getpid()):
        return {"error": f"job.start: root_pid {root_pid} is this engine or one of its ancestors; cancelling it would kill the caller"}
    record = JobRecord(
        job_id=job_id,
        root_pid=root_pid,
        root_ctime=root_ctime,
        lock_path=params.get("lock_path"),
        staging_path=params.get("staging_path"),
        owner_repo=params.get("owner_repo") or "",
        started_at=datetime.now(timezone.utc).isoformat(),
        command=params.get("command"),
    )
    job_record.write_record(record)
    return {"ok": True, "job_id": job_id, "root_pid": root_pid, "root_ctime": root_ctime, "record": str(path)}


def _release(record: JobRecord, repo_root: Path) -> Tuple[bool, bool, List[str]]:
    """Unlink the lock and remove the staging dir; returns (lock_released, staging_removed, notes)."""
    notes: List[str] = []
    lock_released = True
    if record.lock_path:
        try:
            Path(record.lock_path).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            lock_released = False
            notes.append(f"lock {record.lock_path} not removed: {exc}")
    staging_removed = True
    if record.staging_path and os.path.lexists(record.staging_path):
        refusal = job_record.staging_is_removable(record.staging_path, repo_root)
        if refusal is not None:
            staging_removed = False
            notes.append(f"staging not removed: {refusal}")
        else:
            shutil.rmtree(record.staging_path, ignore_errors=True)
            if os.path.lexists(record.staging_path):
                staging_removed = False
                notes.append(f"staging {record.staging_path} not fully removed")
    return lock_released, staging_removed, notes


@register_op("job.cancel")
def _cancel(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "job.cancel": params `job_id` (required), `reason` (optional str).

    Reaps the recorded root's descendants by (pid, ctime) and, when the reap is complete, removes the
    lock and staging and deletes the record. A root that is gone or whose pid was reused reaps nothing
    and still cleans. Replies `{ok, job_id, reaped: [[pid, ctime]...], complete, lock_released,
    staging_removed, already_gone}` plus `reason` when anything was left in place; `{"error": str}`
    for a malformed request, an unknown job, or a tree that holds this engine.
    """
    refusal = validate_params("job.cancel", params, _CANCEL_PARAMS, strict=True)
    if refusal is not None:
        return refusal
    job_id = params["job_id"]
    try:
        record = job_record.read_record(job_id)
    except JobRecordError as exc:
        return {"error": f"job.cancel: {exc}"}
    if record is None:
        return {"error": f"job.cancel: unknown job {job_id}: no record"}
    notes: List[str] = []
    reaped: List[Tuple[int, int]] = []
    complete = True
    already_gone = not _host_probe.alive(record.root_pid, record.root_ctime)
    if not already_gone:
        rows = _host_probe.snapshot()
        if not rows:
            return {"error": "job.cancel: process table unreadable; nothing reaped"}
        tree = _descendants(rows, record.root_pid, record.root_ctime)
        if os.getpid() in {p.pid for p in tree}:
            return {"error": "job.cancel: the recorded tree contains this engine; nothing reaped"}
        if tree:
            killed, complete, why = kill_tree_verified(tree, _host_probe.snapshot, _Kill())
            reaped = [(pid, ctime) for pid, ctime in killed]
            if why:
                notes.append(why)
            if not complete and not why:
                notes.append("a process of the tree could not be terminated")
        else:
            already_gone = True
    reply: dict = {
        "job_id": job_id, "reaped": [list(p) for p in reaped], "complete": complete,
        "lock_released": False, "staging_removed": False, "already_gone": already_gone,
    }
    if not complete:
        notes.append("lock, staging and record left in place; a live writer may hold them")
        reply.update(ok=False, reason="; ".join(notes))
        return reply
    base = Path(repo_root) if repo_root else Path.cwd()
    reply["lock_released"], reply["staging_removed"], release_notes = _release(record, base)
    notes.extend(release_notes)
    if reply["lock_released"] and reply["staging_removed"]:
        job_record.delete_record(job_id)
        reply["ok"] = True
    else:
        reply["ok"] = False
    if notes:
        reply["reason"] = "; ".join(notes)
    return reply
