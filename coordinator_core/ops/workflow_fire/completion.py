"""Engine-owned, restart-recoverable observation of external workflow completions.

The warm watchdog calls ``on_idle_tick``; callers never poll a live run. Disk
records are authoritative, so a successor discovers unfinished deliveries. A
per-record claim serializes watchdog and explicit status delivery attempts.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from coordinator_core.ops.workflow_fire import fire
from coordinator_core.ops.workflow_fire.notify import notify_terminal

_lock = threading.Lock()
_next_tick = 0.0
_scans: dict[Path, object] = {}
_scan_stamps: dict[Path, tuple[int, float]] = {}
_active: dict[Path, None] = {}
_repo_cursor = 0
INTERVAL_SECONDS = 1.0
DISCOVERY_BATCH = 128


def refresh_and_notify(path: Path) -> dict | None:
    """Observe through the fleet's shared kernel-backed notification transaction."""
    from coordinator_core.ops.workflow_fire.notify import refresh_record

    record = fire._read_record(path)
    if not isinstance(record, dict):
        return None
    return refresh_record(path, record, notify=not recipient_ended(record.get("notify_to", "")))


def recipient_ended(target: str) -> bool:
    """Only positive terminal evidence defers a default UUID recipient.

    Explicit/peer routing overrides remain authoritative. An unknown, missing,
    offline or unreadable binding is not terminal evidence and keeps retrying.
    """
    from coordinator_core.agent_pipe import roster
    from coordinator_core.session import external_registry

    try:
        return external_registry.is_ended(target) and roster.resolve(target) is None
    except (OSError, ValueError, TypeError):
        return False  # Unreadable routing is unknown, never confirmed ended.


def _pending(record) -> bool:
    if not isinstance(record, dict) or not record.get("notify_to") or record.get("notified_at"):
        return False
    state = record.get("state")
    if state == "running":
        return isinstance(record.get("pid"), int) and record["pid"] > 0
    return state == "pending" or (state == "exited" and not recipient_ended(record["notify_to"]))


def on_idle_tick(repos, *, clock=time.monotonic) -> bool:
    """Observe pending deliveries once per second, without spawning a process.

    Returns whether pending work needs the warm engine kept alive. Discovery
    advances one bounded directory batch per tick, rotating repos. One active
    delivery is refreshed per tick, rotating active records. Completed records drop out of the active
    set. A terminal record for a known ended recipient stays on disk but does not
    keep the engine alive. Resume or a future warm boot can discover it again.
    Unavailable repos or malformed records cannot stop other deliveries.
    """
    global _next_tick, _repo_cursor
    if not _lock.acquire(blocking=False):
        return bool(_active)
    try:
        now = clock()
        if now < _next_tick:
            return bool(_active)
        _next_tick = now + INTERVAL_SECONDS
        repos = tuple(dict.fromkeys(Path(repo) for repo in repos))
        current = set(repos)
        for retired in _scans.keys() - current:
            scan = _scans.pop(retired)
            close = getattr(scan, "close", None)
            if close:
                close()
        for retired in _scan_stamps.keys() - current:
            _scan_stamps.pop(retired, None)
        # One repo discovery batch and one active delivery per tick bound IPC
        # occupancy independently of fleet size and historical record count.
        selected = repos[_repo_cursor % len(repos)] if repos else None
        _repo_cursor += 1
        for repo in (() if selected is None else (selected,)):
            try:
                directory = fire._registry_dir(str(repo))
                scan = _scans.get(repo)
                stamp = directory.stat().st_mtime_ns if directory.exists() else 0
                cached = _scan_stamps.get(repo)
                # NTFS timestamps may coalesce rapid changes; a periodic rescan
                # bounds recovery even when an atomic record write shares mtime.
                if scan is None and cached and cached[0] == stamp and now < cached[1]:
                    continue
                if scan is None:
                    scan = iter(directory.glob("*.json"))
                    _scans[repo] = scan
                for _ in range(DISCOVERY_BATCH):
                    path = next(scan, None)
                    if path is None:
                        _scans.pop(repo, None)
                        _scan_stamps[repo] = (stamp, now + 30.0)
                        break
                    record = fire._read_record(path)
                    if _pending(record):
                        _active.setdefault(path, None)
            except (OSError, RuntimeError, ValueError):
                continue
        path = next(iter(_active), None)
        if path is not None:
            _active.pop(path, None)
            _active[path] = None
            try:
                record = refresh_and_notify(path)
                if not _pending(record):
                    _active.pop(path, None)
            except OSError:
                pass  # Retry a transient I/O failure on the next rotation.
            except (ValueError, KeyError, TypeError):
                _active.pop(path, None)  # Malformed input is not live work.
        return bool(_active)
    finally:
        _lock.release()
