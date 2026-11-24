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
- proc.stop_own_child ends exactly one pid, never a tree, name or port. The pid must be live, not an
  ancestor of the caller, and either a strict descendant of the worker root or recorded under it
  (`record: true`, stored one file per pid in `<repo>/scratch/spawn-records/<root pid>-<root create_time>/<pid>`
  holding its create_time; a create_time mismatch is a reused pid and is refused). A child reparented
  under the shared serve is reachable only through a record. The worker root is the nearest agent
  session (`codex` or `claude`) ancestor of `caller_pid` (stamped by coordinator-invoke, never trusted
  from the user), or `root_pid` when that is itself an ancestor of the caller. A descendant is admitted
  only when no agent session (the target included) sits between it and the root, so a peer or nested
  session's tree and the session process itself are refused. A cmdline run tag is never admission.
- When neither admits, a pid in `settings_home()/spawn-ledger/<caller_session_id>/` (written at launch by
  the PostToolUse recorder) whose row create_time equals the live one is stopped whatever its parent,
  then its row is deleted. `caller_session_id` is client-stamped and, unlike `caller_pid`, not rebound from
  the transport (`warm.server._bind_trusted_caller_pid`), so it is trusted on the caller's word; empty is refused,
  a create_time mismatch (reused pid) deletes the row and refuses, and a row of another session refuses.
- When the ledger adds nothing (empty refusal), a pid whose inherited environment names `caller_session_id`
  (first non-empty `SESSION_ENV_PRECEDENCE` name, resolved as `warm.caller_context` does) is stopped: a child
  inherits it through every shell hop, so it outlives the shell. Never consulted for a pid under another agent
  session, a session process, the serving engine and its ancestors, or one that started before the worker root; one `environ()` read, AccessDenied refuses.
- A record is refused for a pid older than the root or under any agent session (`codex`, `claude`)
  other than the root; record dirs of dead roots are reaped on the next record. Within those bounds a
  record is trusted on the caller's word.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from coordinator_core.ipc import register_op

SCRATCH_OP = "scratch.remove"
STOP_OP = "proc.stop_own_child"
ALLOWED_ROOTS = ("scratch/codex-tmp", "scratch/tmp")
_TOMBSTONES = "scratch/.deleted"
_RECORDS = "scratch/spawn-records"
_SESSION_NAMES = frozenset({"codex", "claude"})
_ROOT_NAMES = _SESSION_NAMES
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


def _record_dir(repo_root: Any, root: Any) -> Path:
    return Path(repo_root) / _RECORDS / f"{root.pid}-{int(root.create_time())}"


def _read_record(directory: Path, pid: int) -> Any:
    try:
        return float((directory / str(pid)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_record(directory: Path, pid: int, created: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{pid}.{os.getpid()}.tmp"
    tmp.write_text(repr(created), encoding="utf-8")
    os.replace(tmp, directory / str(pid))


def _reap_dead_roots(repo_root: Any, ps: Any, keep: Path) -> None:
    """Drop record dirs whose root is gone or reused; one file per pid keeps concurrent records lossless."""
    base = Path(repo_root) / _RECORDS
    try:
        entries = [e for e in base.iterdir() if e.is_dir() and e != keep]
    except OSError:
        return
    for entry in entries:
        pid_s, _, ct_s = entry.name.partition("-")
        try:
            alive = int(ct_s) == int(ps.Process(int(pid_s)).create_time())
        except ps.NoSuchProcess:
            alive = False
        except (ValueError, ps.AccessDenied):
            continue
        if not alive:
            shutil.rmtree(entry, ignore_errors=True)


def _record_hint(pid: int) -> str:
    return f"record it first: coordinator-invoke proc.stop_own_child '{{\"pid\":{pid},\"record\":true}}'"


def _record(target: Any, root: Any, repo_root: Any, ps: Any) -> dict:
    """Record `target` under `root`: only a process that started after the root, and is not under
    another agent session, can be its launch."""
    if not repo_root:
        return {"refused": "no repo root for the spawn record"}
    created = target.create_time()
    if created < root.create_time():
        return {"refused": "pid started before the worker root; it cannot be its launch"}
    for anc in _ancestors(ps, target.pid)[1:]:
        if anc.pid != root.pid and os.path.splitext(anc.name().lower())[0] in _SESSION_NAMES:
            return {"refused": "pid belongs to another agent session"}
    directory = _record_dir(repo_root, root)
    _write_record(directory, target.pid, created)
    _reap_dead_roots(repo_root, ps, directory)
    return {"recorded": target.pid}


def _recorded_refusal(target: Any, root: Any, repo_root: Any) -> str:
    """'' when `target` is recorded under `root` with its recorded create_time; else the refusal."""
    base = "pid is not a descendant of the worker root"
    if not repo_root:
        return base
    directory = _record_dir(repo_root, root)
    recorded = _read_record(directory, target.pid)
    if recorded is None:
        return f"{base} and has no spawn record under it; {_record_hint(target.pid)}"
    if recorded != target.create_time():
        (directory / str(target.pid)).unlink(missing_ok=True)
        return f"{base}; spawn record create_time differs (pid reused)"
    return ""


def _worker_root(ps: Any, chain: list, root_pid: Any) -> tuple[Any, str]:
    """(root process, refusal); the root is `root_pid` or the nearest agent-session ancestor of the caller."""
    if root_pid is not None:
        for p in chain:
            if p.pid == root_pid:
                return p, ""
        return None, "root_pid is not an ancestor of the caller"
    named = [p for p in chain if os.path.splitext(p.name().lower())[0] in _ROOT_NAMES]
    if not named:
        return None, "no agent-session (codex or claude) ancestor of the caller; pass root_pid (your shell's own pid)"
    return named[0], ""


def _is_session(proc: Any) -> bool:
    return os.path.splitext(proc.name().lower())[0] in _SESSION_NAMES


def _descends_from(ps: Any, pid: int, root: Any) -> tuple[bool, str]:
    """(descends, refusal): `pid` strictly under `root` with no agent session (itself included) between."""
    chain = _ancestors(ps, pid)
    pids = [p.pid for p in chain]
    if root.pid not in pids[1:]:
        return False, ""
    if any(_is_session(p) for p in chain[:pids.index(root.pid)]):
        return False, "pid is or sits under another agent session"
    return True, ""


def _ledger_admits(target: Any, caller_session_id: Any) -> tuple[bool, str]:
    """(admitted, refusal): admitted when `target` is in the caller session's spawn ledger with its
    recorded create_time. The refusal is '' when the ledger has nothing to add to the earlier
    refusal; a reused pid's row is deleted. An unstamped session (None, a direct caller of the API)
    adds nothing; the client always stamps one, '' when its environment names none."""
    from coordinator_core.ops import _spawn_ledger as ledger

    if caller_session_id is None:
        return False, ""
    if not isinstance(caller_session_id, str) or not caller_session_id:
        return False, "caller session unknown; run through coordinator-invoke"
    row = ledger.read_row(caller_session_id, target.pid)
    if row is None:
        foreign = ledger.find_row(target.pid) is not None
        return False, "pid belongs to another session's spawn ledger" if foreign else ""
    if row.create_time != target.create_time():
        ledger.delete_row(caller_session_id, target.pid)
        return False, "spawn ledger create_time differs (pid reused)"
    return True, ""


def _serving_engine_pids() -> set:
    """The engine process serving this call and its ancestors; the warm server inherits the session env
    of whichever session spawned it and is shared by every peer, so its env names no one caller."""
    import psutil

    pids = {os.getpid()}
    try:
        pids.update(p.pid for p in psutil.Process().parents())
    except psutil.Error:
        pass
    return pids


def _env_admits(target: Any, caller_session_id: Any, root: Any = None) -> tuple[bool, str]:
    """(admitted, refusal): admitted when the session id in `target`'s inherited environment, resolved
    as `warm.caller_context` resolves a request env, equals `caller_session_id`. A child inherits it
    through every shell and launcher hop, so it survives an exited parent. One `environ()` read; an
    unstamped or empty session, a scrubbed env, the serving engine (or its ancestors) and a pid that started
    before the worker root (a shared daemon the session merely inherited) admit nothing."""
    import psutil
    from coordinator_core.session.core import SESSION_ENV_PRECEDENCE, canonical_session_env_value

    if not isinstance(caller_session_id, str) or not caller_session_id:
        return False, ""
    if target.pid in _serving_engine_pids():
        return False, ""
    if root is None or target.create_time() < root.create_time():
        return False, ""
    try:
        env = target.environ()
    except psutil.AccessDenied:
        return False, "access denied"
    except psutil.NoSuchProcess:
        return False, ""
    vendor = env.get("COORDINATOR_AGENT_VENDOR", "external")
    for name in SESSION_ENV_PRECEDENCE:
        value = env.get(name)
        if value:
            return canonical_session_env_value(name, value, vendor=vendor) == caller_session_id, ""
    return False, ""


def stop_own_child(
    pid: Any, caller_pid: Any, root_pid: Any = None, ps: Any = None, record: bool = False, repo_root: Any = None,
    caller_session_id: Any = None,
) -> dict:
    """Stop one pid the caller owns; `{"stopped": pid}` or `{"refused": reason}`.

    Owned = a strict descendant of the worker root, or a pid recorded under that root (same
    create_time) by an earlier `record=True` call, or, when neither admits, a pid in the caller
    session's spawn ledger with its recorded create_time, whatever its current parent, or, when
    the ledger has nothing to say, a pid whose inherited environment names the caller's session.
    `record=True` records `pid` and stops nothing: `{"recorded": pid}`.
    """
    if ps is None:
        import psutil as ps
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return {"refused": "pid must be an integer above 1"}
    if not isinstance(caller_pid, int) or caller_pid <= 1:
        return {"refused": "caller pid unknown; run through coordinator-invoke"}
    try:
        chain = _ancestors(ps, caller_pid)
        root_proc, why = _worker_root(ps, chain, root_pid)
        if root_proc is None and root_pid is not None:
            return {"refused": why}
        if pid in {p.pid for p in chain}:
            return {"refused": "pid is the caller or one of its ancestors"}
        if record:
            return _record(ps.Process(pid), root_proc, repo_root, ps) if root_proc else {"refused": why}
        target = ps.Process(pid)
        other_session = False
        if root_proc is None:
            reason = why
        else:
            under, reason = _descends_from(ps, pid, root_proc)
            other_session = bool(reason)
            if not under and not reason:
                reason = _recorded_refusal(target, root_proc, repo_root)
        ledgered = False
        if reason:
            ledgered, ledger_reason = _ledger_admits(target, caller_session_id)
            if not ledgered:
                if ledger_reason or other_session or _is_session(target):
                    return {"refused": ledger_reason or reason}
                env_ok, env_reason = _env_admits(target, caller_session_id, root_proc)
                if not env_ok:
                    return {"refused": env_reason or reason}
        target.terminate()
        try:
            target.wait(_GRACE_S)
        except ps.TimeoutExpired:
            target.kill()
        if ledgered:
            from coordinator_core.ops import _spawn_ledger as ledger

            ledger.delete_row(caller_session_id, pid)
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
    """JSON-RPC "proc.stop_own_child": params `pid` (int), optional `root_pid`, optional `record`; `caller_pid` is client-stamped.

    `caller_session_id` is client-stamped too. `record: true` records `pid` as the caller's own
    launch instead of stopping it. A pid the root and record paths refuse is stopped when the
    caller's session ledgered it (same create_time), else when its inherited environment names that
    session (survives an exited shell or launcher hop)."""
    return stop_own_child(
        params.get("pid"), params.get("caller_pid"), params.get("root_pid"),
        record=params.get("record") is True, repo_root=repo_root,
        caller_session_id=params.get("caller_session_id"))
