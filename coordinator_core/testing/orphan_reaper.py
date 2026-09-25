"""coordinator_core.testing.orphan_reaper — session-scoped basetemp orphan reap (R1).

WHY THIS EXISTS. A fast/full-tier run can leave a detached child (a sleeper,
a stray daemon a test forgot to join) parented to init once the test process
that spawned it exits — the 4-day orphan named in
`docs/plans/2026-09-07-fix-the-validate-gate-recursive-tier-invocation.md`.
This module makes that reap structural for the repo-root regime instead of
relying on an operator noticing a stale `ps` line days later.

MECHANISM
    `pytest_sessionfinish` runs once, in the controller process only (never a
    worker — an xdist worker's own `pytest_sessionfinish` would otherwise
    race the controller's reap with no idempotence key). It reads the
    session's basetemp directly off `config._tmp_path_factory._basetemp`
    (private, but the only route that does not itself create a directory —
    `TempPathFactory.getbasetemp()` creates one on a miss). A `None` basetemp
    means no test used `tmp_path`/`tmp_path_factory` this session; the hook
    returns immediately and never calls `getbasetemp()`.

    Scan is two-stage to keep `psutil.process_iter()` cheap: stage one keeps
    only same-user processes newer than this pytest process's own
    `create_time()` (a reliable "started during this session" proxy — this
    process necessarily started before anything it could have spawned);
    only THOSE survivors pay for `cwd()`/`cmdline()`. A process is an orphan
    when its cwd, or any cmdline token, has `basetemp`'s own path components
    as a strict prefix (`Path.parts` comparison — a component-boundary
    match, so `.../pytest-2` never matches `.../pytest-20`). This process and
    every ancestor of it are excluded unconditionally, so a reap can never
    turn on the shell or CI runner that launched pytest itself.

    Reap is terminate-then-kill: `terminate()` every orphan, `wait_procs`
    up to 1s, `kill()` whatever is still alive. One loud stderr line per
    reaped process (pid + argv), plus one summary line carrying the scan's
    own `time.process_time()` delta — the visibility this hook exists to
    provide, since it changes nothing else observable.

NEGATIVE SPEC
    - Never calls `config._tmp_path_factory.getbasetemp()` — that call
      creates a directory as a side effect, and a session that used no
      temp path must not gain one just by finishing.
    - Never changes `exitstatus`. A teardown reap must not become a new gate
      failure mode; a broken scan here is swallowed, not raised, past this
      hook.
    - Does not run in an xdist worker (`config.workerinput` presence gates
      it) — the controller is the only registrant, so there is exactly one
      call per session and no idempotence key is needed.
    - Does not reap outside `basetemp`, does not replace the wall-clock kill
      ceiling `with-suite-mutex --max-runtime` already owns, does not
      register as a fixture (nothing here is scoped per-test), and is not a
      general process-management utility — one hook, pure helpers under it.

Registration: the repo-root `conftest.py` re-exports `pytest_sessionfinish`
by name. That is the only registration site — see its own NEGATIVE SPEC
amendment for why `coordinator_core/conftest.py` is deliberately not a
second one.

Spec backlink: docs/plans/2026-09-07-fix-the-validate-gate-recursive-tier-invocation.md (R1)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None

__all__ = [
    "pytest_sessionfinish",
    "is_worker",
    "get_basetemp",
    "is_under_basetemp",
    "collect_ancestor_pids",
    "find_orphans",
    "reap_processes",
]


def is_worker(config) -> bool:
    """Whether `config` belongs to an xdist worker rather than the controller."""
    return getattr(config, "workerinput", None) is not None


def get_basetemp(config) -> Optional[str]:
    """Read the session's basetemp without ever creating one.

    Returns `None` when no test in this session used `tmp_path`/
    `tmp_path_factory` — deliberately reads the private `_basetemp` attr
    rather than calling `getbasetemp()`, which creates a directory on a miss.
    """
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is None:
        return None
    basetemp = getattr(factory, "_basetemp", None)
    if basetemp is None:
        return None
    return str(basetemp)


def is_under_basetemp(path_str: Optional[str], basetemp_parts: Sequence[str]) -> bool:
    """Whether `path_str` has `basetemp_parts` as a strict path-component prefix.

    A component comparison (`Path.parts`), not a string prefix — so
    `.../pytest-2` never matches `.../pytest-20`.
    """
    if not path_str:
        return False
    try:
        parts = Path(path_str).parts
    except Exception:
        return False
    n = len(basetemp_parts)
    if n == 0:
        return False
    return parts[:n] == tuple(basetemp_parts)


def collect_ancestor_pids(pid: int) -> set:
    """The pids of every ancestor of `pid` (never including `pid` itself)."""
    ancestors: set = set()
    if psutil is None:
        return ancestors
    try:
        proc = psutil.Process(pid)
        for anc in proc.parents():
            ancestors.add(anc.pid)
    except Exception:
        pass
    return ancestors


def find_orphans(
    basetemp: str,
    own_pid: int,
    session_start_time: float,
    own_username: Optional[str],
) -> List["psutil.Process"]:
    """Two-stage scan for same-user, session-new processes rooted under `basetemp`.

    Stage one (cheap): same user, `create_time() >= session_start_time`,
    excluding this process and every one of its ancestors. Stage two
    (expensive): only for stage-one survivors, read `cwd()`/`cmdline()` and
    keep those with a basetemp-rooted path-component match on either.
    `AccessDenied`/`NoSuchProcess` are ignored at every step — a process that
    vanished or is unreadable is not an orphan this reap can act on.
    """
    if psutil is None:
        return []
    basetemp_parts = Path(basetemp).parts
    exclude_pids = collect_ancestor_pids(own_pid) | {own_pid}

    survivors = []
    for proc in psutil.process_iter():
        if proc.pid in exclude_pids:
            continue
        try:
            if own_username is not None and proc.username() != own_username:
                continue
            if proc.create_time() < session_start_time:
                continue
        except (psutil.AccessDenied, psutil.NoSuchProcess, Exception):
            continue
        survivors.append(proc)

    orphans = []
    for proc in survivors:
        try:
            cwd = proc.cwd()
        except (psutil.AccessDenied, psutil.NoSuchProcess, Exception):
            cwd = None
        try:
            cmdline = proc.cmdline()
        except (psutil.AccessDenied, psutil.NoSuchProcess, Exception):
            cmdline = []
        matched = is_under_basetemp(cwd, basetemp_parts) or any(
            is_under_basetemp(tok, basetemp_parts) for tok in cmdline
        )
        if matched:
            orphans.append(proc)
    return orphans


def reap_processes(procs: Sequence["psutil.Process"]) -> List[Tuple[int, List[str]]]:
    """Terminate every proc in `procs`, then kill whatever is still alive after 1s.

    Returns `(pid, argv)` pairs for every process this call attempted to
    reap, captured before termination so a dead process's argv is still
    reportable.
    """
    if psutil is None:
        return []
    reaped: List[Tuple[int, List[str]]] = []
    procs = list(procs)
    for proc in procs:
        try:
            argv = proc.cmdline()
        except Exception:
            argv = []
        reaped.append((proc.pid, argv))
        try:
            proc.terminate()
        except Exception:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=1)
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            pass
    return reaped


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001 - exitstatus never touched
    """Reap any process orphaned under this session's basetemp. Never raises, never
    touches `exitstatus` — a broken scan must not become a new gate failure mode."""
    try:
        config = session.config
        if is_worker(config):
            return
        if psutil is None:
            return
        basetemp = get_basetemp(config)
        if basetemp is None:
            return

        own_pid = os.getpid()
        own_proc = psutil.Process(own_pid)
        session_start_time = own_proc.create_time()
        try:
            own_username = own_proc.username()
        except Exception:
            own_username = None

        scan_start = time.process_time()
        orphans = find_orphans(basetemp, own_pid, session_start_time, own_username)
        reaped = reap_processes(orphans)
        scan_ms = (time.process_time() - scan_start) * 1000.0

        for pid, argv in reaped:
            print(f"[orphan-reaper] reaped pid={pid} argv={argv!r}", file=sys.stderr)
        print(
            f"[orphan-reaper] scan_process_ms={scan_ms:.2f} reaped={len(reaped)}",
            file=sys.stderr,
        )
    except Exception:
        return
