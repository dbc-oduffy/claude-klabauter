"""Shared-tree build guard for app-session validate: a per-worktree lock and a launch-handle conflict check.

Pure functions over injected inputs. `is_live(pid, start_epoch) -> bool` is supplied by the
caller; nothing here probes processes.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

IsLive = Callable[[int, float], bool]

VALIDATE_KEY_SUFFIX = "@validate"


@dataclass(frozen=True)
class LockResult:
    """acquired: this call holds the lock. holder: the live holder's record when not acquired."""

    acquired: bool
    holder: Optional[dict] = None
    path: Optional[Path] = field(default=None)


def _norm(p: Any) -> str:
    return os.path.normcase(str(Path(p).resolve()))


def lock_path(handle_dir: Path, worktree: Any) -> Path:
    digest = hashlib.sha256(_norm(worktree).encode("utf-8")).hexdigest()[:16]
    return Path(handle_dir) / f"validate-{digest}.lock"


def _read(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _try_create(path: Path, record: dict) -> bool:
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    return True


def _holder_live(holder: Optional[dict], is_live: IsLive) -> bool:
    if not holder:
        return False
    try:
        return bool(is_live(int(holder["pid"]), float(holder["start_epoch"])))
    except (KeyError, TypeError, ValueError):
        return False


def acquire_tree_lock(
    handle_dir: Path, worktree: Any, pid: int, start_epoch: float, is_live: IsLive
) -> LockResult:
    """O_EXCL-create the worktree's validate lock; reclaim once from a dead holder."""
    handle_dir = Path(handle_dir)
    handle_dir.mkdir(parents=True, exist_ok=True)
    path = lock_path(handle_dir, worktree)
    record = {
        "pid": pid,
        "start_epoch": start_epoch,
        "worktree": _norm(worktree),
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }
    if _try_create(path, record):
        return LockResult(True, None, path)
    holder = _read(path)
    if _holder_live(holder, is_live):
        return LockResult(False, holder, path)
    aside = path.with_name(f"{path.name}.stale-{pid}-{os.urandom(4).hex()}")
    try:
        os.rename(path, aside)
    except FileNotFoundError:
        pass
    except OSError:
        return LockResult(False, _read(path), path)
    else:
        try:
            aside.unlink()
        except OSError:
            pass
    if _try_create(path, record):
        return LockResult(True, None, path)
    return LockResult(False, _read(path), path)


def transfer_tree_lock(path: Path, pid: int, start_epoch: float) -> bool:
    """Re-stamp the lock with the runner's identity; returns False when the lock is gone."""
    path = Path(path)
    record = _read(path)
    if record is None:
        return False
    record["pid"] = pid
    record["start_epoch"] = start_epoch
    tmp = path.with_name(f"{path.name}.tmp-{pid}-{os.urandom(4).hex()}")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)
    return True


def release_tree_lock(path: Path, pid: int) -> bool:
    """Remove the lock only when this pid holds it."""
    path = Path(path)
    record = _read(path)
    if record is None or record.get("pid") != pid:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def check_launch_conflicts(
    handles: Mapping[str, Mapping[str, Any]],
    worktree: Any,
    build_isolated: bool,
    is_live: IsLive,
) -> list:
    """Live launch handles whose cwd is the worktree, excluding `@validate` keys; [] when build_isolated."""
    if build_isolated:
        return []
    target = _norm(worktree)
    out = []
    for key, h in handles.items():
        if str(key).endswith(VALIDATE_KEY_SUFFIX):
            continue
        cwd = h.get("cwd")
        if not cwd or _norm(cwd) != target:
            continue
        if _holder_live(dict(h), is_live):
            out.append({"key": key, **dict(h)})
    return out
