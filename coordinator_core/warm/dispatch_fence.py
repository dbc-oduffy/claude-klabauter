"""Kill fence for a dispatch whose worker the accept process terminated.

The accept process writes `<claim_dir>/<key>.killed` before it terminates an overdue worker; a
worker that survives (blocked in a kernel call) then finds its own dispatch fenced and its
mutating ops (`ceremony.commit_v2`) refuse. The worker side keeps a process-wide holder of
the key it is running, set and cleared around each dispatch (pool workers are reused).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

_SUFFIX = ".killed"

#: A fence older than this has outlived any worker it could still stop; `write` sweeps them so a
#: long-lived engine keeps a bounded fence directory.
_STALE_SECS = 3600.0

_holder: Optional[tuple] = None


def _fence_path(claim_dir, key: str) -> Path:
    return Path(claim_dir) / f"{key}{_SUFFIX}"


def write(claim_dir, key: str) -> bool:
    """Create the fence file. False when it cannot be written (never raises)."""
    try:
        fd = os.open(_fence_path(claim_dir, key), os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError:
        return False
    os.close(fd)
    _sweep_stale(claim_dir)
    return True


def _sweep_stale(claim_dir) -> None:
    cutoff = time.time() - _STALE_SECS
    try:
        for entry in Path(claim_dir).glob(f"*{_SUFFIX}"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def is_fenced(claim_dir, key: str) -> bool:
    """One stat: True iff `key`'s dispatch was reported killed."""
    try:
        return _fence_path(claim_dir, key).exists()
    except OSError:
        return False


def hold(claim_dir, key: Optional[str]) -> None:
    """Record the dispatch this process is running; a no-op without a claim dir and key."""
    global _holder
    _holder = (claim_dir, key) if claim_dir and key else None


def release() -> None:
    global _holder
    _holder = None


def current_dispatch_fenced() -> bool:
    """True iff this process runs a dispatch that carries a kill fence."""
    held = _holder
    return held is not None and is_fenced(held[0], held[1])
