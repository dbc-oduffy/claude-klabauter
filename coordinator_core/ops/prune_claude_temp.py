"""coordinator_core.ops.prune_claude_temp — prune stale Claude Code per-session
temp dirs under `<tempdir>/claude/<project>/<session>`.

Run detached (`spawn_detached`) by the SessionStart async dispatcher; also runnable
as `python prune_claude_temp.py [session_id] [repo_root] [--drain]`. A session dir is
deleted only when nothing inside it (nor the dir itself) was modified within
`RETENTION_DAYS`. A 24h stamp file throttles real runs. Never raises.

With a repo root, also prunes the repo's gitignored scratch spaces (`REPO_SPACES`) at
unit depth 1: symlinks and junctions are never followed or deleted through, and any
unit containing a git-tracked path is kept.

Also prunes the fleet root and the repo's coordinator root (`temp_layout`) by mtime
age, plus a top-level pass over the system Temp dir for legacy coordinator names and
empty `.tmpXXXXXX` dirs. All passes share a process-time budget; a pass that stops on
it leaves its stamp unwritten so the next run resumes. `--drain` ignores budget and
throttle.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from coordinator_core.temp_layout import (
    coordinator_temp_root,
    fleet_temp_root,
    is_legacy_top_level,
)

GENERATES = []

RETENTION_DAYS = 7
THROTTLE_SECONDS = 24 * 3600
STAMP_NAME = ".prune-stamp"
REPO_SPACES = ("scratch", "tasks", ".coordinator-local/subagent-share")
BUDGET_SECONDS = 0.3
_REPARSE_POINT = 0x400
_EMPTY_TMP_RE = re.compile(r"^\.tmp[A-Za-z0-9]{6}$")
_AUTONOMOUS_PREFIX = "autonomous-run-"
_PYTEST_CHILD = "pytest"


class _Budget:
    """Process-time budget shared by every pass of one run; `limit=None` is unbounded."""

    def __init__(self, limit: "Optional[float]" = BUDGET_SECONDS) -> None:
        self.limit = limit
        self._start = time.process_time()
        self.stopped = False

    def check(self) -> bool:
        """Latch and return True once the budget is spent."""
        if self.limit is not None and time.process_time() - self._start >= self.limit:
            self.stopped = True
        return self.stopped


def claude_temp_root() -> Path:
    return Path(tempfile.gettempdir()) / "claude"


def stamp_is_fresh(root: Path, now: Optional[float] = None) -> bool:
    """True when the throttle stamp exists and is under 24h old."""
    try:
        age = (time.time() if now is None else now) - (root / STAMP_NAME).stat().st_mtime
    except OSError:
        return False
    return age < THROTTLE_SECONDS


def _is_link(path: str) -> bool:
    """Symlink or Windows junction/reparse point (never followed)."""
    try:
        if os.path.islink(path):
            return True
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & _REPARSE_POINT)
    except OSError:
        return True


def _gitignored(repo_root: str, space: str) -> bool:
    """True when .gitignore has a line naming the space or its top-level dir."""
    try:
        text = Path(repo_root, ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    top = space.split("/")[0]
    names = {top, top + "/", "/" + top, "/" + top + "/", space, space + "/", "/" + space, "/" + space + "/"}
    return any(line.strip() in names for line in text.splitlines())


def find_repo_root(cwd: str) -> str:
    """Nearest ancestor of `cwd` holding a `.git` directory; '' when none."""
    if not cwd:
        return ""
    try:
        cur = Path(cwd).resolve()
    except OSError:
        return ""
    for d in (cur, *cur.parents):
        if (d / ".git").is_dir():
            return str(d)
    return ""


def _is_under(path: Path, repo_root: str) -> bool:
    try:
        Path(path).relative_to(repo_root)
        return True
    except ValueError:
        return False


def _read_index(repo_root: str) -> "Optional[bytes]":
    try:
        return Path(repo_root, ".git", "index").read_bytes()
    except OSError:
        return None


def _has_fresh_entry(path: str, cutoff: float) -> bool:
    """True when `path` or anything beneath it has mtime >= cutoff (early exit)."""
    try:
        if os.stat(path).st_mtime >= cutoff:
            return True
        if not os.path.isdir(path) or os.path.islink(path):
            return False
    except OSError:
        return True  # unreadable: treat as live, keep it
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                        return True
                    if entry.is_dir(follow_symlinks=False) and not _is_link(entry.path):
                        stack.append(entry.path)
        except OSError:
            return True
    return False


def _skip_error(*_args) -> None:
    return None


def _rmtree_tolerant(path: str) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_skip_error)
    else:
        shutil.rmtree(path, onerror=_skip_error)


def _reap_tombstones(path: str, budget: "_Budget") -> int:
    """Remove every child of a scratch tombstone dir whatever its age; links are unlinked, never followed."""
    removed = 0
    if _is_link(path):
        return 0
    try:
        children = list(os.scandir(path))
    except OSError:
        return 0
    for child in children:
        if budget.stopped:
            break
        try:
            if _is_link(child.path) or not child.is_dir(follow_symlinks=False):
                os.unlink(child.path)
            else:
                _rmtree_tolerant(child.path)
            removed += 1
        except OSError:
            continue
        finally:
            budget.check()
    return removed


def _prune_level(
    path: str,
    level: int,
    depth: int,
    cutoff: float,
    session_id: str,
    tracked: "Optional[tuple[bytes, str]]" = None,
    budget: "Optional[_Budget]" = None,
    keep: "frozenset[str]" = frozenset(),
) -> int:
    """Delete stale units at `depth` below the root; rmdir emptied intermediates.

    `keep` names level-0 children never touched. At least one child is examined
    before the budget can stop the scan."""
    removed = 0
    try:
        children = list(os.scandir(path))
    except OSError:
        return 0
    for child in children:
        if budget is not None and budget.stopped:
            break
        if level == 0 and child.name in keep:
            continue
        try:
            # TRAP: DirEntry.is_dir(follow_symlinks=False) is True for a Windows
            # junction, so without this skip an intermediate junction is recursed
            # into and the target's stale children are deleted.
            if _is_link(child.path):
                continue
            is_dir = child.is_dir(follow_symlinks=False)
            if is_dir and level < depth - 1:
                removed += _prune_level(
                    child.path, level + 1, depth, cutoff, session_id, tracked, budget
                )
                try:
                    os.rmdir(child.path)  # succeeds only when empty
                except OSError:
                    pass
                continue
            if session_id and child.name == session_id:
                continue
            if tracked is not None:
                if child.name == STAMP_NAME:
                    continue
                rel = os.path.relpath(child.path, tracked[1]).replace(os.sep, "/")
                if rel.encode("utf-8", "surrogateescape") in tracked[0]:
                    continue
            if _has_fresh_entry(child.path, cutoff):
                continue
            if is_dir:
                _rmtree_tolerant(child.path)
            else:
                os.unlink(child.path)
            removed += 1
        except OSError:
            continue
        finally:
            if budget is not None:
                budget.check()
    return removed


def default_roots(repo_root: str = "") -> "list[tuple[Path, int, float]]":
    """(root path, unit depth below root, retention days) for every pruned space.

    Repo spaces are listed only when they are real directories that .gitignore
    names (read as text; no git spawn). The fleet root and the repo's coordinator
    root are pruned at depth 1."""
    roots = [
        (claude_temp_root(), 2, RETENTION_DAYS),
        (fleet_temp_root(), 1, RETENTION_DAYS),
    ]
    if repo_root:
        try:
            roots.append((coordinator_temp_root(repo_root), 1, RETENTION_DAYS))
        except ValueError:
            pass
        for space in REPO_SPACES:
            p = Path(repo_root, *space.split("/"))
            if p.is_dir() and not _is_link(str(p)) and _gitignored(repo_root, space):
                roots.append((p, 1, RETENTION_DAYS))
    return roots


def _top_level_stamp_root() -> Path:
    return fleet_temp_root().parent


def _prune_top_level(
    cutoff: float,
    budget: "Optional[_Budget]" = None,
    keep: "frozenset[str]" = frozenset(),
) -> int:
    """One scandir of system Temp: stale legacy-named entries and stale empty `.tmpXXXXXX` dirs.

    `keep` names entries never touched (live sessions' legacy autonomous-run sentinels,
    still honoured by the dual-read in `autonomous_sentinel`)."""
    removed = 0
    try:
        entries = list(os.scandir(tempfile.gettempdir()))
    except OSError:
        return 0
    for entry in entries:
        if budget is not None and budget.stopped:
            break
        try:
            name = entry.name
            if name in keep:
                continue
            legacy = is_legacy_top_level(name)
            if not legacy and not _EMPTY_TMP_RE.match(name):
                continue
            if _is_link(entry.path):
                continue
            if legacy:
                if _has_fresh_entry(entry.path, cutoff):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    _rmtree_tolerant(entry.path)
                else:
                    os.unlink(entry.path)
                removed += 1
            elif entry.is_dir(follow_symlinks=False):
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
                os.rmdir(entry.path)  # fails unless empty
                removed += 1
        except OSError:
            continue
        finally:
            if budget is not None:
                budget.check()
    return removed


def roots_due(
    roots: "Optional[list[tuple[Path, int, float]]]" = None, repo_root: str = ""
) -> bool:
    if roots is None and not stamp_is_fresh(_top_level_stamp_root()):
        return True
    return any(
        r.is_dir() and not stamp_is_fresh(r)
        for r, _d, _n in (default_roots(repo_root) if roots is None else roots)
    )


def prune(
    session_id: str = "",
    *,
    roots: "Optional[list[tuple[Path, int, float]]]" = None,
    now: Optional[float] = None,
    repo_root: str = "",
    drain: bool = False,
    budget_seconds: "Optional[float]" = BUDGET_SECONDS,
    live_session_ids: "frozenset[str]" = frozenset(),
) -> int:
    """Prune stale units under each due root; returns the number removed.

    The top-level Temp pass runs only when `roots` is None (the default set). A pass
    stamps only when it finished inside the budget. `drain` ignores budget and
    throttle. `autonomous-run-<sid>` in the fleet root is kept for the current and
    any `live_session_ids` session."""
    now = time.time() if now is None else now
    budget = _Budget(None if drain else budget_seconds)
    live = set(live_session_ids)
    if session_id:
        live.add(session_id)
    keep_live = frozenset(_AUTONOMOUS_PREFIX + sid for sid in live)
    fleet = fleet_temp_root()
    coord_parent = fleet.parent
    removed = 0
    index = None
    for root, depth, retention_days in default_roots(repo_root) if roots is None else roots:
        if budget.stopped:
            break
        root = Path(root)
        if not root.is_dir() or (not drain and stamp_is_fresh(root, now)):
            continue
        tracked = None
        if repo_root and _is_under(root, repo_root):
            if _is_link(str(root)):
                continue
            index = _read_index(repo_root) if index is None else index
            if index is None:
                continue  # cannot prove nothing is tracked: keep everything
            tracked = (index, repo_root)
        keep: "frozenset[str]" = frozenset()
        if root == fleet:
            keep = keep_live
        elif root.parent == coord_parent:
            keep = frozenset({_PYTEST_CHILD})
        elif repo_root and root == Path(repo_root, "scratch"):
            from coordinator_core.ops.scratch_delete import TOMBSTONE_DIRNAME

            keep = frozenset({TOMBSTONE_DIRNAME})
            removed += _reap_tombstones(str(root / TOMBSTONE_DIRNAME), budget)
        removed += _prune_level(
            str(root), 0, depth, now - retention_days * 86400, session_id, tracked, budget, keep
        )
        if not budget.stopped:
            try:
                (root / STAMP_NAME).touch()
            except OSError:
                pass
    if roots is None and not budget.stopped:
        stamp_root = _top_level_stamp_root()
        if drain or not stamp_is_fresh(stamp_root, now):
            removed += _prune_top_level(now - RETENTION_DAYS * 86400, budget, keep_live)
            if not budget.stopped:
                try:
                    stamp_root.mkdir(parents=True, exist_ok=True)
                    (stamp_root / STAMP_NAME).touch()
                except OSError:
                    pass
    return removed


def main(argv: "list[str]") -> int:
    args = [a for a in argv[1:] if a != "--drain"]
    try:
        prune(
            args[0] if args else "",
            repo_root=args[1] if len(args) > 1 else "",
            drain="--drain" in argv[1:],
        )
    except Exception:  # noqa: BLE001 -- housekeeping child never fails loud
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
