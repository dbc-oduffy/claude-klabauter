"""Engine-owned supervisor stop of a run's headless workflow fire.

`workflow.cancel` calls `stop_run_fires`: it finds the run's fire records (`staged_stall.fire_records_for_run`),
verifies each recorded driver PID is still that fire's driver, then ends its process tree leaf-first with the
driver last. This is the engine stopping its own spawned child, not a user kill by name, port or tree; it runs
in-process through psutil with no shell and no taskkill.

Identity is argv plus creation time: a PID whose command line is not
`-m coordinator_core.ops.workflow_fire.headless --record <that record>`, or whose creation time differs from the
record's `pid_birth`, is some other process and is never touched.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

DRIVER_MODULE = "coordinator_core.ops.workflow_fire.headless"
BIRTH_TOLERANCE_S = 1.0
# One total grace for every tree level so a deep tree stays inside the cancel op's 500ms budget.
GRACE_BUDGET_S = 0.3
MAX_PASSES = 3


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _is_fire_driver(argv: list, record_path: Path) -> bool:
    if DRIVER_MODULE not in argv or "--record" not in argv:
        return False
    at = argv.index("--record") + 1
    return at < len(argv) and _same_path(str(argv[at]), str(record_path))


def _depths(procs: list, root_pid: int) -> dict:
    """pid -> distance from the driver, over `procs` (a recursive child listing)."""
    ppid = {p.pid: p.ppid() for p in procs}
    out = {}
    for p in procs:
        depth, cur = 1, p.pid
        while ppid.get(cur) not in (root_pid, None) and depth < 64:
            cur = ppid[cur]
            depth += 1
        out[p.pid] = depth
    return out


def _end(ps: Any, procs: list) -> list:
    """Terminate `procs`, then kill what outlives the shared grace; returns the survivors."""
    for p in procs:
        try:
            p.terminate()
        except ps.NoSuchProcess:
            pass
        except ps.AccessDenied:
            pass
    _, alive = ps.wait_procs(procs, timeout=GRACE_BUDGET_S)
    for p in alive:
        try:
            p.kill()
        except (ps.NoSuchProcess, ps.AccessDenied):
            pass
    if alive:
        _, alive = ps.wait_procs(alive, timeout=GRACE_BUDGET_S)
    return list(alive)


def stop_fire(record_path: Path, record: dict, ps: Any = None) -> dict:
    """Stop one fire. `{"stopped": [pids], "survivors": [pids], "skipped": reason|None}`."""
    if ps is None:
        import psutil as ps
    pid, birth = record.get("pid"), record.get("pid_birth")
    out: dict = {"stopped": [], "survivors": [], "skipped": None}
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        out["skipped"] = "record names no driver pid"
        return out
    try:
        driver = ps.Process(pid)
        argv = list(driver.cmdline())
        created = driver.create_time()
    except ps.NoSuchProcess:
        out["skipped"] = "driver not running"
        return out
    except ps.AccessDenied:
        out["survivors"].append(pid)
        out["skipped"] = "driver identity unreadable"
        return out
    if not _is_fire_driver(argv, record_path):
        out["skipped"] = "pid is not this fire's driver (argv mismatch)"
        return out
    if not isinstance(birth, (int, float)) or abs(created - birth) > BIRTH_TOLERANCE_S:
        out["skipped"] = "pid is not this fire's driver (creation time mismatch)"
        return out
    seen: set = set()
    try:
        for _ in range(MAX_PASSES):
            kids = [c for c in driver.children(recursive=True) if c.pid not in seen]
            if not kids:
                break
            depth = _depths(kids, pid)
            for level in sorted(set(depth.values()), reverse=True):
                group = [c for c in kids if depth[c.pid] == level]
                out["survivors"].extend(p.pid for p in _end(ps, group))
                seen.update(c.pid for c in group)
        out["survivors"].extend(p.pid for p in _end(ps, [driver]))
    except ps.NoSuchProcess:
        pass
    out["survivors"] = sorted(set(out["survivors"]))
    out["stopped"] = sorted(p for p in seen | {pid} if p not in out["survivors"])
    return out


def stop_run_fires(repo_root: Path, run_id: str, ps: Any = None) -> dict:
    """Stop every live fire of `run_id`. `{"stopped": [pids], "survivors": [pids]}`."""
    from coordinator_core.ops.dispatch_emit import staged_stall

    stopped: list = []
    survivors: list = []
    for record_path, record in staged_stall.fire_records_for_run(repo_root, run_id):
        if record.get("state") == "exited":
            continue
        result = stop_fire(Path(record_path), record, ps)
        stopped.extend(result["stopped"])
        survivors.extend(result["survivors"])
    return {"stopped": sorted(set(stopped)), "survivors": sorted(set(survivors))}
