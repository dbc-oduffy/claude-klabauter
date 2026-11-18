"""Reap op for `multiprocessing` spawn-pool workers orphaned by a dead, caller-named parent.

`spawn_orphans.reap` takes the pid a session recorded at spawn, refuses while that parent is alive,
and otherwise stops only python processes whose argv carries `spawn_main` and the exact element
`parent_pid=<pid>`. One `psutil.process_iter` pass, no subprocess spawns, no name, port or tree kill.
"""

from __future__ import annotations

import re
import time

from coordinator_core.ipc import CallerFacingValidationError, register_op

OP_NAME = "spawn_orphans.reap"
_PARENT_PID_RE = re.compile(r"parent_pid=(\d+)(?!\d)")
_TERMINATE_GRACE_SECS = 1.0
_KILL_GRACE_SECS = 0.5
_CMDLINE_REPLY_CHARS = 200

_SCHEMA = {
    "type": "object",
    "properties": {
        "parent_pid": {"type": "integer", "minimum": 1},
        "min_age_s": {"type": "number", "minimum": 0},
        "dry_run": {"type": "boolean"},
    },
    "required": ["parent_pid"],
    "additionalProperties": False,
}


def _validated(params) -> tuple[int, float, bool]:
    p: dict = params if isinstance(params, dict) else {}
    unknown = set(p) - {"parent_pid", "min_age_s", "dry_run"}
    if unknown:
        raise CallerFacingValidationError(f"{OP_NAME}: unknown fields {sorted(unknown)}")
    parent = p.get("parent_pid")
    if not isinstance(parent, int) or isinstance(parent, bool) or parent < 1:
        raise CallerFacingValidationError(f"{OP_NAME}: parent_pid must be a positive integer")
    floor = p.get("min_age_s", 0)
    if not isinstance(floor, (int, float)) or isinstance(floor, bool) or floor < 0:
        raise CallerFacingValidationError(f"{OP_NAME}: min_age_s must be a non-negative number")
    dry = p.get("dry_run", False)
    if not isinstance(dry, bool):
        raise CallerFacingValidationError(f"{OP_NAME}: dry_run must be a boolean")
    return parent, float(floor), dry


def _parent_birth(psutil, pid: int) -> tuple[bool, float | None]:
    """(alive, create_time): a pid whose birth cannot be read counts alive, so an unreadable parent refuses."""
    if not psutil.pid_exists(pid):
        return False, None
    try:
        return True, psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return False, None
    except Exception:  # noqa: BLE001 -- unreadable birth: assume the parent lives
        return True, None


def _names_parent(argv: list[str], parent_pid: int) -> bool:
    """`spawn_main` in some element and an exact `parent_pid=<parent_pid>` token: `parent_pid=<P>0` is another pid."""
    if not any("spawn_main" in a for a in argv):
        return False
    wanted = str(parent_pid)
    return any(m == wanted for a in argv for m in _PARENT_PID_RE.findall(a))


def _workers_of(psutil, parent_pid: int) -> list[tuple]:
    """(Process, pid, create_time, cmdline) of each live python `spawn_main` process naming `parent_pid`.

    Trap: `create_time` is read for matches only; as an iter attribute it costs ~4ms a process on Windows."""
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            argv = proc.cmdline()
            if not _names_parent(argv, parent_pid):
                continue
            born = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        found.append((proc, proc.info["pid"], born, " ".join(argv)))
    return found


def _stop(psutil, procs: list) -> tuple[list, dict]:
    """Terminate, wait, kill the stubborn; (gone processes, {pid: reason} for those still alive or unsignalable)."""
    failed: dict[int, str] = {}
    signalled = []
    for proc in procs:
        try:
            proc.terminate()
            signalled.append(proc)
        except psutil.NoSuchProcess:
            signalled.append(proc)
        except psutil.AccessDenied:
            failed[proc.pid] = "access denied"
    gone, alive = psutil.wait_procs(signalled, timeout=_TERMINATE_GRACE_SECS)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            failed[proc.pid] = "access denied"
    if alive:
        more_gone, still = psutil.wait_procs(alive, timeout=_KILL_GRACE_SECS)
        gone = list(gone) + list(more_gone)
        for proc in still:
            failed.setdefault(proc.pid, "survived kill")
    return list(gone), failed


@register_op(OP_NAME, params_schema=_SCHEMA)
def reap(params, repo_root=None, *, psutil_module=None) -> dict:
    """Stop the python `spawn_main` workers that name a dead `parent_pid`.

    Params: `parent_pid` (int, required: the pid the caller recorded at spawn), `min_age_s` (number,
    optional: workers younger than this are left and listed under `young`), `dry_run` (bool: match, stop nothing).
    Reply: `{ok, parent_pid, dry_run, matched, stopped, failed, young}` (`matched` is `[{pid, cmdline}]`,
    `stopped` pids, `failed` `[{pid, reason}]`), or `{ok: false, refused: "parent_alive", parent_pid, stopped: []}`
    with nothing signalled while a process holds `parent_pid` and was born no later than a worker.
    Trap: a worker younger than a live process at `parent_pid` is a reused pid's leftover and is still reaped.
    """
    parent_pid, floor, dry_run = _validated(params)
    psutil = psutil_module
    if psutil is None:
        import psutil

    alive, parent_born = _parent_birth(psutil, parent_pid)
    workers = _workers_of(psutil, parent_pid)
    if alive and not (
        workers
        and all(parent_born is not None and w[2] is not None and parent_born > w[2] for w in workers)
    ):
        return {"ok": False, "refused": "parent_alive", "parent_pid": parent_pid, "stopped": []}

    now = time.time()
    young = [w for w in workers if floor and (w[2] is None or now - w[2] < floor)]
    eligible = [w for w in workers if w not in young]
    matched = [{"pid": w[1], "cmdline": w[3][:_CMDLINE_REPLY_CHARS]} for w in eligible]
    reply = {
        "ok": True, "parent_pid": parent_pid, "dry_run": dry_run, "matched": matched,
        "stopped": [], "failed": [], "young": [w[1] for w in young],
    }
    if dry_run or not eligible:
        return reply
    gone, failed = _stop(psutil, [w[0] for w in eligible])
    reply["stopped"] = sorted(p.pid for p in gone)
    reply["failed"] = [{"pid": pid, "reason": why} for pid, why in sorted(failed.items())]
    return reply
