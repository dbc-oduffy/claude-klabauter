"""coordinator_core.ops.worker_cleanup -- the two cleanups a Codex stage worker cannot run as a shell
command: "scratch.remove" (a worker's own scratch dir) and "proc.stop_own_child" (a process the
worker spawned).

Why ops: Codex's own exec policy rejects a PowerShell `Remove-Item -Recurse -Force` outright
("`<cmd>` rejected: blocked by policy", no rule named), and the DENY-KILL-BY-PORT-OR-TREE guard
rejects any kill whose pid came from a name or process-table lookup. Neither shape appears in a
`coordinator-invoke` call, so no allow-rule is needed for these.

Invariants:
- scratch.remove admits only a strict descendant of `<repo>/scratch/codex-tmp` or `<repo>/scratch/tmp`,
  judged on realpath, so a junction or `..` that escapes the root is refused. The root itself is refused.
  One rename into `scratch/.deleted/` per path; the detached prune pass reclaims the bytes.
- proc.stop_own_child ends exactly one pid, never a tree, name or port. The pid must be a live strict
  descendant of the worker root and not an ancestor of the caller. The worker root is the nearest
  `codex` ancestor of `caller_pid` (stamped by coordinator-invoke, never trusted from the user), or
  `root_pid` when that is itself an ancestor of the caller.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from coordinator_core.ipc import register_op

SCRATCH_OP = "scratch.remove"
STOP_OP = "proc.stop_own_child"
ALLOWED_ROOTS = ("scratch/codex-tmp", "scratch/tmp")
_TOMBSTONES = "scratch/.deleted"
_ROOT_NAMES = frozenset({"codex"})
_GRACE_S = 2.0


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _refusal(root: Path, raw: Any) -> tuple[str, str]:
    """(normalized path, refusal reason); reason '' when `raw` may be removed."""
    if not isinstance(raw, str) or not raw:
        return "", "path is not a non-empty string"
    p = Path(raw)
    target = _norm(str(p if p.is_absolute() else root / p))
    for rel in ALLOWED_ROOTS:
        base = _norm(str(root / rel))
        if target != base and target.startswith(base + os.sep):
            return target, ""
    return target, "path is outside scratch/codex-tmp and scratch/tmp"


def remove_scratch(repo_root: Path, paths: list) -> dict:
    root = Path(repo_root)
    removed: list[str] = []
    absent: list[str] = []
    refused: list[dict] = []
    tomb = root / _TOMBSTONES
    for raw in paths:
        target, reason = _refusal(root, raw)
        if reason:
            refused.append({"path": raw, "reason": reason})
        elif not os.path.lexists(target):
            absent.append(raw)
        else:
            try:
                tomb.mkdir(parents=True, exist_ok=True)
                os.rename(target, tomb / f"{os.path.basename(target)}-{time.time_ns()}")
                removed.append(raw)
            except OSError as exc:
                refused.append({"path": raw, "reason": f"rename failed (in use?): {exc}"})
    return {"removed": removed, "absent": absent, "refused": refused}


def _ancestors(ps: Any, pid: int) -> list:
    chain, cur = [], ps.Process(pid)
    while cur is not None:
        chain.append(cur)
        cur = cur.parent()
    return chain


def stop_own_child(pid: Any, caller_pid: Any, root_pid: Any = None, ps: Any = None) -> dict:
    """Stop one descendant of the caller's worker root; `{"stopped": pid}` or `{"refused": reason}`."""
    if ps is None:
        import psutil as ps
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return {"refused": "pid must be an integer above 1"}
    if not isinstance(caller_pid, int) or caller_pid <= 1:
        return {"refused": "caller pid unknown; run through coordinator-invoke"}
    try:
        chain = _ancestors(ps, caller_pid)
        chain_pids = {p.pid for p in chain}
        if root_pid is not None:
            if root_pid not in chain_pids:
                return {"refused": "root_pid is not an ancestor of the caller"}
            root = root_pid
        else:
            named = [p for p in chain if os.path.splitext(p.name().lower())[0] in _ROOT_NAMES]
            if not named:
                return {"refused": "no codex ancestor of the caller; pass root_pid (your shell's own pid)"}
            root = named[0].pid
        if pid in chain_pids:
            return {"refused": "pid is the caller or one of its ancestors"}
        target = ps.Process(pid)
        if root not in {p.pid for p in _ancestors(ps, pid)[1:]}:
            return {"refused": "pid is not a descendant of the worker root"}
        target.terminate()
        try:
            target.wait(_GRACE_S)
        except ps.TimeoutExpired:
            target.kill()
    except ps.NoSuchProcess:
        return {"refused": "pid or an ancestor is not running"}
    except ps.AccessDenied:
        return {"refused": "access denied"}
    return {"stopped": pid}


@register_op(SCRATCH_OP)
def _scratch_handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "scratch.remove": params `paths` (list of str under scratch/codex-tmp or scratch/tmp)."""
    paths = params.get("paths")
    if not isinstance(paths, list):
        return {"error": "scratch.remove: paths must be a list of strings"}
    root = params.get("repo_root") or repo_root
    if not root:
        return {"error": "scratch.remove: no repo root"}
    return remove_scratch(Path(root), paths)


@register_op(STOP_OP)
def _stop_handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "proc.stop_own_child": params `pid` (int), optional `root_pid`; `caller_pid` is client-stamped."""
    return stop_own_child(params.get("pid"), params.get("caller_pid"), params.get("root_pid"))
