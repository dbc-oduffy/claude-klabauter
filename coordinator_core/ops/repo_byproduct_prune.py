"""coordinator_core.ops.repo_byproduct_prune -- the per-repo byproduct prune runner.

Fired detached from the SessionStart async dispatcher (`spawn_if_due`), also runnable as
`python repo_byproduct_prune.py [--force] [--dry-run]` from inside a repo. One run walks
`repo_byproduct_policy.CATEGORIES` and deletes the units `select_victims` names, bounded by
`BUDGET_SECONDS` of process time. Spawn-free: os.scandir/lstat only.

A stamp in `.coordinator-local` debounces runs and is written only when a run finished; a run
that exhausts its budget leaves it unwritten and resumes at the next session start. A unit
chosen for deletion but not finished is recorded in an inflight file and resumed first next
run: deleting children bumps a directory's mtime, so a half-deleted unit would otherwise look
young. Never raises.
"""

from __future__ import annotations

import fnmatch
import json
import os
import stat
import sys
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

GENERATES = []

THROTTLE_SECONDS = 7 * 24 * 3600
LOCK_STALE_SECONDS = 3600
BUDGET_SECONDS = 0.4
STAMP_NAME = "byproduct-prune.stamp"
LOCK_NAME = "byproduct-prune.lock"
INFLIGHT_NAME = "byproduct-prune.inflight.json"
_LOCAL = ".coordinator-local"
_SCRIPT = "coordinator_core/ops/repo_byproduct_prune.py"
_DAY = 86400.0
_MAX_GLOB_DEPTH = 2
_CHECK_EVERY = 64
_OLD, _FRESH = "old", "fresh"

# Process-time source; tests substitute a deterministic clock.
_clock = time.process_time


@lru_cache(maxsize=1)
def _deps() -> tuple:
    """Heavy imports, deferred so the hook-path `spawn_if_due` imports stdlib only."""
    from coordinator_core.ops import prune_claude_temp, repo_byproduct_policy, repo_scratch_sweep

    return prune_claude_temp, repo_byproduct_policy, repo_scratch_sweep


class _Exhausted(Exception):
    """The process-time budget ran out; the run stops and leaves the stamp unwritten."""


class _Budget:
    def __init__(self, limit: float) -> None:
        self.limit = float(limit)
        self.end = _clock() + self.limit

    def left(self) -> float:
        return self.end - _clock()

    def expired(self) -> bool:
        return _clock() >= self.end

    def roomy(self) -> bool:
        """True while at least half the budget remains. A step that starts roomy and still
        runs out is too big for any budget, so its caller decides instead of retrying forever."""
        return self.left() >= self.limit / 2

    def step(self) -> "tuple[float, bool]":
        """`(stop_at, roomy)` for one scan or sizing walk. A roomy walk may spend half the
        budget, so a unit too big to finish still leaves room to delete it."""
        roomy = self.roomy()
        return (_clock() + self.limit / 2 if roomy else self.end), roomy

    def require(self) -> None:
        if self.expired():
            raise _Exhausted


@dataclass
class _U:
    abs: str
    rel: str
    mtime: float
    is_dir: bool
    fresh: bool = False
    blocked: Optional[bool] = None


@dataclass
class _Ctx:
    repo_root: str
    now: float
    apply: bool
    budget: _Budget
    index: Optional[bytes]
    ignore_lines: frozenset
    inflight: dict
    deleted: dict
    skipped: dict
    prefix_hit: dict


def _local_path(repo_root: str, name: str) -> str:
    return os.path.join(repo_root, _LOCAL, name)


def due(repo_root: str, now: "Optional[float]" = None) -> bool:
    """True when the stamp is missing or older than `THROTTLE_SECONDS`. One stat."""
    try:
        age = (time.time() if now is None else now) - os.stat(_local_path(str(repo_root), STAMP_NAME)).st_mtime
    except FileNotFoundError:
        return True
    except Exception:  # noqa: BLE001 -- unreadable stamp: let the run decide
        return True
    return age >= THROTTLE_SECONDS


def _acquire(lock: str, now: float) -> bool:
    try:
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            if now - os.stat(lock).st_mtime < LOCK_STALE_SECONDS:
                return False
            os.unlink(lock)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError:
            return False
    except OSError:
        return False
    os.close(fd)
    return True


def _unlink_any(path: str) -> None:
    """Remove a file, link or junction; a read-only file is made writable once. Best effort."""
    try:
        os.unlink(path)
        return
    except FileNotFoundError:
        return
    except OSError:
        pass
    try:
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)
    except OSError:
        try:
            os.rmdir(path)
        except OSError:
            pass


def _remove(path: str, budget: _Budget) -> bool:
    """Delete `path` (a link is unlinked, never descended). Each removed file is permanent
    progress; returns False when the budget ran out before the tree was gone."""
    rss = _deps()[2]
    p = _deps()[0]._long(path)
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return True
    if rss._is_link(st) or not stat.S_ISDIR(st.st_mode):
        _unlink_any(p)
        return True
    dirs, stack, n = [p], [p], 0
    while stack:
        cur = stack.pop()
        if budget.expired():
            return False
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            n += 1
            if n % _CHECK_EVERY == 0 and budget.expired():
                return False
            try:
                est = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(est.st_mode) and not rss._is_link(est):
                dirs.append(e.path)
                stack.append(e.path)
            else:
                _unlink_any(e.path)
    for d in reversed(dirs):
        n += 1
        if n % _CHECK_EVERY == 0 and budget.expired():
            return False
        try:
            os.rmdir(d)
        except OSError:
            pass
    return True


def _scan_age(u: _U, cutoff: float, budget: _Budget) -> str:
    """`_FRESH` when anything beneath `u` is at or past `cutoff`, else `_OLD`. Breadth-first so
    shallow activity shows first. A scan that outruns a roomy budget reads as old: a tree too
    big to scan in half a budget is not a live working set, and a verdict is what lets the
    delete make progress."""
    rss = _deps()[2]
    if not u.is_dir:
        return _OLD
    stop, roomy = budget.step()
    queue, n = deque([_deps()[0]._long(u.abs)]), 0
    while queue:
        if _clock() >= stop:
            if roomy:
                return _OLD
            raise _Exhausted
        cur = queue.popleft()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except FileNotFoundError:
            continue
        except OSError:
            return _FRESH
        for e in entries:
            n += 1
            if n % _CHECK_EVERY == 0 and _clock() >= stop:
                if roomy:
                    return _OLD
                raise _Exhausted
            try:
                est = e.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                return _FRESH
            if est.st_mtime >= cutoff:
                return _FRESH
            if stat.S_ISDIR(est.st_mode) and not rss._is_link(est):
                queue.append(e.path)
    return _OLD


def _size(u: _U, limit: int, budget: _Budget, on_timeout: int) -> int:
    """Bytes under `u`, stopping once the total reaches `limit` (the caller's cap headroom:
    reaching it already decides the unit). Running out of a roomy budget returns `on_timeout`."""
    rss = _deps()[2]
    if not u.is_dir:
        try:
            return 0 if rss._is_link(os.lstat(u.abs)) else os.lstat(u.abs).st_size
        except OSError:
            return 0
    stop, roomy = budget.step()
    total, n = 0, 0
    stack = [_deps()[0]._long(u.abs)]
    while stack:
        if _clock() >= stop:
            if roomy:
                return on_timeout
            raise _Exhausted
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            n += 1
            if n % _CHECK_EVERY == 0 and _clock() >= stop:
                if roomy:
                    return on_timeout
                raise _Exhausted
            try:
                est = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if rss._is_link(est):
                continue
            if stat.S_ISDIR(est.st_mode):
                stack.append(e.path)
            else:
                total += est.st_size
                if total >= limit:
                    return total
    return total


def _entry_unit(e: "os.DirEntry", rel: str) -> "Optional[_U]":
    try:
        st = e.stat(follow_symlinks=False)
    except OSError:
        return None
    return _U(e.path, rel, st.st_mtime, stat.S_ISDIR(st.st_mode) and not _deps()[2]._is_link(st))


def _child_units(parent: _U, rel: str, pat: str, budget: _Budget) -> "list[_U]":
    """Direct children of `parent` named like `pat`, links included and never descended."""
    budget.require()
    try:
        with os.scandir(parent.abs) as it:
            entries = list(it)
    except OSError:
        return []
    kids = (_entry_unit(e, f"{rel}/{e.name}") for e in entries if fnmatch.fnmatchcase(e.name, pat))
    return [k for k in kids if k]


def _list_units(cat, root: str, budget: _Budget) -> "list[_U]":
    """Units of `cat` under `root`. `**/<pat>` (or `**/<parent>/<child>`, units being the children) looks at most `_MAX_GLOB_DEPTH` levels down and
    never into links; `scratch/warp` is a container whose children are the units."""
    rss = _deps()[2]
    glob, units = cat.unit_glob, []
    if glob.startswith("**/"):
        pat, _, child_pat = glob[3:].partition("/")
        level = [(root, cat.relpath)]
        for depth in range(_MAX_GLOB_DEPTH + 1):
            nxt = []
            for d, rel in level:
                budget.require()
                try:
                    with os.scandir(d) as it:
                        entries = list(it)
                except OSError:
                    continue
                for e in entries:
                    erel = f"{rel}/{e.name}"
                    if fnmatch.fnmatchcase(e.name, pat):
                        u = _entry_unit(e, erel)
                        if u and child_pat:
                            if u.is_dir:
                                units.extend(_child_units(u, erel, child_pat, budget))
                        elif u:
                            units.append(u)
                    elif depth < _MAX_GLOB_DEPTH:
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISDIR(st.st_mode) and not rss._is_link(st):
                            nxt.append((e.path, erel))
            level = nxt
        return units
    budget.require()
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return units
    for e in entries:
        if not fnmatch.fnmatchcase(e.name, glob):
            continue
        u = _entry_unit(e, f"{cat.relpath}/{e.name}")
        if u is None:
            continue
        if cat.relpath == "scratch" and e.name == "warp" and u.is_dir:
            budget.require()
            try:
                with os.scandir(e.path) as it:
                    kids = list(it)
            except OSError:
                continue
            for k in kids:
                ku = _entry_unit(k, f"scratch/warp/{k.name}")
                if ku:
                    units.append(ku)
            continue
        units.append(u)
    return units


def _blocked(ctx: _Ctx, cat, u: _U) -> bool:
    """True for a unit that must stay: a warp run not `landed`, or a path the git index names."""
    if u.blocked is None:
        parts = u.rel.split("/")
        live = (
            len(parts) == 3 and parts[1] == "warp" and parts[0] in ("state", "scratch")
            and not _deps()[2]._run_is_terminal(ctx.repo_root, parts[2])
        )
        u.blocked = live or _tracked(ctx, cat, u)
    return u.blocked


def _tracked(ctx: _Ctx, cat, u: _U) -> bool:
    index = ctx.index
    if index is None:
        return True
    hit = ctx.prefix_hit.get(cat.name)
    if hit is None:
        hit = ctx.prefix_hit[cat.name] = (cat.relpath + "/").encode("utf-8") in index
    if not hit:
        return False
    return (u.rel + ("/" if u.is_dir else "")).encode("utf-8") in index


def _delete(ctx: _Ctx, cat, u: _U) -> None:
    if not ctx.apply:
        ctx.deleted[cat.name] = ctx.deleted.get(cat.name, 0) + 1
        return
    key = (cat.name, u.rel)
    ctx.inflight[key] = True
    if not _remove(u.abs, ctx.budget):
        raise _Exhausted
    ctx.inflight.pop(key, None)
    ctx.deleted[cat.name] = ctx.deleted.get(cat.name, 0) + 1


def _purge_inflight(ctx: _Ctx, cat) -> None:
    for key in [k for k in ctx.inflight if k[0] == cat.name]:
        ctx.inflight.pop(key, None)


def _resume_inflight(ctx: _Ctx, cat) -> None:
    for key in [k for k in ctx.inflight if k[0] == cat.name]:
        rel = key[1]
        parts = rel.split("/")
        if not rel.startswith(cat.relpath + "/") or ".." in parts:
            ctx.inflight.pop(key, None)
            continue
        path = os.path.join(ctx.repo_root, *parts)
        try:
            st = os.lstat(path)
        except OSError:
            ctx.inflight.pop(key, None)
            continue
        u = _U(path, rel, st.st_mtime, stat.S_ISDIR(st.st_mode) and not _deps()[2]._is_link(st))
        if _blocked(ctx, cat, u):
            ctx.inflight.pop(key, None)
            continue
        _delete(ctx, cat, u)


def _prune_category(ctx: _Ctx, cat) -> None:
    policy = _deps()[1]
    root = os.path.join(ctx.repo_root, *cat.relpath.split("/"))
    try:
        rst = os.lstat(root)
    except OSError:
        _purge_inflight(ctx, cat)
        return
    if _deps()[2]._is_link(rst) or not stat.S_ISDIR(rst.st_mode):
        ctx.skipped[cat.name] = "link"
    elif cat.gitignore_line not in ctx.ignore_lines:
        ctx.skipped[cat.name] = "not_ignored"
    elif ctx.index is None:
        ctx.skipped[cat.name] = "no_index"
    else:
        ctx.budget.require()
        _resume_inflight(ctx, cat)
        units = _list_units(cat, root, ctx.budget)
        keep = max(cat.keep_newest, 0)
        cutoff = ctx.now - cat.max_age_days * _DAY
        ordered = sorted(units, key=lambda u: u.mtime, reverse=True)
        survivors = ordered[:keep]
        for u in ordered[keep:]:
            if u.mtime < cutoff and not _blocked(ctx, cat, u):
                if _scan_age(u, cutoff, ctx.budget) == _OLD:
                    _delete(ctx, cat, u)
                    continue
                u.fresh = True
            survivors.append(u)
        sized, kept = [], 0
        for i, u in enumerate(survivors):
            if kept > cat.max_total_bytes:
                sized.append(policy.Unit(u.rel, u.mtime, 0))
                continue
            protected = i < keep
            limit = cat.max_total_bytes - kept + 1
            b = _size(u, limit, ctx.budget, 0 if protected else limit)
            sized.append(policy.Unit(u.rel, u.mtime, b))
            if protected or kept + b <= cat.max_total_bytes:
                kept += b
        by_rel = {u.rel: u for u in survivors}
        for v in policy.select_victims(sized, cat, ctx.now):
            u = by_rel[v.path]
            if not u.fresh and not _blocked(ctx, cat, u):
                _delete(ctx, cat, u)
        return
    _purge_inflight(ctx, cat)


def _load_inflight(repo_root: str) -> dict:
    try:
        with open(_local_path(repo_root, INFLIGHT_NAME), encoding="utf-8") as fh:
            return {(str(c), str(r)): True for c, r in json.load(fh)}
    except (OSError, ValueError, TypeError):
        return {}


def _save_inflight(repo_root: str, inflight: dict) -> None:
    path = _local_path(repo_root, INFLIGHT_NAME)
    try:
        if not inflight:
            os.unlink(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(inflight), fh)
        os.replace(tmp, path)
    except OSError:
        pass


def run_prune(
    repo_root,
    now: "Optional[float]" = None,
    apply: bool = True,
    force: bool = False,
    budget: float = BUDGET_SECONDS,
) -> dict:
    """Prune every policy category of `repo_root` once, within `budget` process seconds.

    Returns `{"ran": True, "deleted": {category: units}, "skipped": {category: reason},
    "process_ms": float}` on a finished run; `{"ran": False}` when the stamp is fresh (and
    `force` unset) or another run holds the lock; `{"ran": False, "budget_exhausted": True}`
    when the budget ran out, with the stamp untouched. `apply=False` counts what would go and
    changes nothing. Never raises."""
    result: dict = {"ran": False}
    try:
        root = str(repo_root)
        now = time.time() if now is None else now
        if not force and not due(root, now):
            return result
        lock = _local_path(root, LOCK_NAME)
        if apply and not _acquire(lock, now):
            return result
        try:
            prune_claude_temp, policy, _ = _deps()
            clock_start = _clock()
            try:
                lines = Path(root, ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            ctx = _Ctx(
                repo_root=root, now=now, apply=apply, budget=_Budget(budget),
                index=prune_claude_temp._read_index(root),
                ignore_lines=frozenset(ln.strip() for ln in lines),
                inflight=_load_inflight(root), deleted={}, skipped={}, prefix_hit={},
            )
            try:
                for cat in policy.CATEGORIES:
                    _prune_category(ctx, cat)
            except _Exhausted:
                if apply:
                    _save_inflight(root, ctx.inflight)
                result["budget_exhausted"] = True
                return result
            if apply:
                _save_inflight(root, ctx.inflight)
                Path(_local_path(root, STAMP_NAME)).touch()
            result.update(
                ran=True, deleted=ctx.deleted, skipped=ctx.skipped,
                process_ms=round((_clock() - clock_start) * 1000, 1),
            )
        finally:
            if apply:
                try:
                    os.unlink(lock)
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001 -- unattended housekeeping never fails loud
        result["error"] = repr(exc)
    return result


def spawn_if_due(repo_root: str, spawn: "Optional[Callable[..., bool]]" = None) -> bool:
    """Stamp check only; spawns the detached run when due. Never raises; True when spawned."""
    try:
        if not due(repo_root):
            return False
        if spawn is None:
            from coordinator_core.ops.ceremony.detached_spawn import spawn_detached as spawn
        return bool(spawn(str(repo_root), _SCRIPT, []))
    except Exception:  # noqa: BLE001
        return False


def main(argv: "list[str]") -> int:
    try:
        root = _deps()[0].find_repo_root(os.getcwd())
        if root:
            result = run_prune(root, force="--force" in argv[1:], apply="--dry-run" not in argv[1:])
            if "--dry-run" in argv[1:]:
                print(json.dumps(result, default=str))
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
