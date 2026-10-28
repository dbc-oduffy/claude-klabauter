"""coordinator_core.ops.retention_run -- the one unattended retention entry point.

Fired detached from the SessionStart async dispatcher and from the memory governor
(`spawn_if_due`), also runnable as `python retention_run.py [--force]`. One run applies, in
order: the transcript retention rule (`cruft_sweep`, live sessions exempt), the regenerable
cache caps (`cache_caps`), then the Temp/claude per-session prune (`prune_claude_temp`).

A stamp file in the Claude home debounces real runs; the stamp is written only when the run
finished, so a failed run retries. A lock file keeps two runs from overlapping. Unresolved
liveness keeps every transcript. Never raises.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

GENERATES = []

THROTTLE_SECONDS = 6 * 3600
LOCK_STALE_SECONDS = 3600
STAMP_NAME = ".retention-stamp"
LOCK_NAME = ".retention-lock"
_SCRIPT = "coordinator_core/ops/retention_run.py"


def _home(claude_home: "Optional[Path]") -> Path:
    if claude_home is not None:
        return Path(claude_home)
    from coordinator_core.ops.claude_home_census import claude_home as _ch

    return _ch()


def due(claude_home: "Optional[Path]" = None, now: "Optional[float]" = None) -> bool:
    """True when the debounce stamp is missing or older than `THROTTLE_SECONDS`. One stat."""
    try:
        stamp = _home(claude_home) / STAMP_NAME
        age = (time.time() if now is None else now) - stamp.stat().st_mtime
    except FileNotFoundError:
        return True
    except Exception:  # noqa: BLE001 -- unreadable stamp: let the run decide
        return True
    return age >= THROTTLE_SECONDS


def _acquire(lock: Path, now: float) -> bool:
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if now - lock.stat().st_mtime < LOCK_STALE_SECONDS:
                return False
            lock.unlink()
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    except OSError:
        return False
    os.close(fd)
    return True


def _transcripts(home: Path, live: "Optional[frozenset]", apply: bool) -> "tuple[int, int]":
    from coordinator_core.ops import claude_home_census as census
    from coordinator_core.ops import cruft_sweep as cs

    if live is None:
        return 0, 0
    max_bytes = census.transcript_max_bytes()
    max_age = int(census.transcript_max_age_seconds())
    tuples = cs._walk_uuid_artifacts(home / "projects", (), cs._Watchdog(None))
    candidates = cs._transcript_retention_candidates(
        tuples, live_ids=live, max_bytes=max_bytes, max_age_secs=max_age, now=int(time.time()),
    )
    return cs._sweep_transcript_retention(
        candidates, now=int(time.time()), apply=apply, json_mode=False, quiet=True,
        emit_fn=None, watchdog=cs._Watchdog(None), max_bytes=max_bytes, max_age_secs=max_age,
    )


def run_retention(
    claude_home: "Optional[Path]" = None,
    temp_root: "Optional[Path]" = None,
    live_session_ids: "Optional[frozenset]" = None,
    apply: bool = True,
    force: bool = False,
) -> dict:
    """Run transcript retention, cache caps and the Temp/claude prune once.

    `live_session_ids=None` resolves the box-wide live set; an unresolvable set keeps every
    transcript and the Temp/claude root. Returns `{"ran": bool, ...}`; `ran` is False when the
    stamp is fresh (and `force` unset) or another run holds the lock. A non-None `temp_root`
    only suppresses the Temp/claude prune (fixture runs); it is never itself pruned."""
    result: dict = {"ran": False}
    try:
        home = _home(claude_home)
        now = time.time()
        if not force and not due(home, now):
            return result
        lock = home / LOCK_NAME
        if not _acquire(lock, now):
            return result
        try:
            live = live_session_ids
            if live is None:
                from coordinator_core.ops.cruft_sweep import box_live_session_ids

                live = box_live_session_ids()
            result["transcript_bytes"], result["transcript_items"] = _transcripts(home, live, apply)
            from coordinator_core.ops import cache_caps

            # An explicit claude_home is a fixture: cap only its own classes, never the
            # real settings home or warm dirs.
            classes = (
                cache_caps.CACHE_CLASSES if claude_home is None
                else [c for c in cache_caps.CACHE_CLASSES if c.base == "claude_home"]
            )
            result["cache_rows"] = len(
                cache_caps.apply_caps(claude_home=home, apply=apply, classes=classes)
            )
            if apply and temp_root is None:
                from coordinator_core.ops import prune_claude_temp

                prune_claude_temp.prune("", live_session_ids=live)
            result["ran"] = True
            if apply:
                (home / STAMP_NAME).touch()
        finally:
            try:
                lock.unlink()
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001 -- unattended housekeeping never fails loud
        result["error"] = repr(exc)
    return result


def spawn_if_due(
    repo_root: str,
    claude_home: "Optional[Path]" = None,
    spawn: "Optional[Callable[..., bool]]" = None,
) -> bool:
    """Stamp check only; spawns the detached run when due. Never raises; True when spawned."""
    try:
        if not due(claude_home):
            return False
        if spawn is None:
            from coordinator_core.ops.ceremony.detached_spawn import spawn_detached as spawn
        return bool(spawn(str(repo_root), _SCRIPT, []))
    except Exception:  # noqa: BLE001
        return False


def main(argv: "list[str]") -> int:
    run_retention(force="--force" in argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
