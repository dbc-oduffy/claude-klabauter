"""
coordinator_core.ops.poll_scratch_dir — op "fanout.poll_scratch_dir": block until a
scratch directory holds at least N entries, or a timeout elapses.

Purpose: waiting primitive for fan-out dispatch — the orchestrator polls a wave's
scratch directory until the expected number of per-agent result files have landed
(or the budget runs out), replacing the bash `$SECONDS`-builtin loop the fence
inventory flagged as Windows-unavailable.

Contract (op-classification manifest row `poll-scratch-dir-until-count-or-timeout`):
    params: {scratch_dir: str, min_count: int, timeout_seconds: float,
             poll_interval_seconds: float}
        -> {status: "count_reached"|"timeout", count: int, elapsed_seconds: float}
    or {"error": <str>} when a param is missing/invalid or scratch_dir does not
    exist (structured error, cartography.churn precedent).

Design (wave-3 settlement B6, RATIFIES — binding):
    - `time.monotonic()` deadline loop (CC-2 — never wall-clock for elapsed);
      count = `len(list(os.scandir(scratch_dir)))` per tick;
      `time.sleep(poll_interval_seconds)` between ticks.
    - Missing `scratch_dir` -> structured error, NOT a zero count: absence is a
      premise failure, not an empty result. This also holds if the directory
      vanishes mid-loop.
    - This op INTENTIONALLY BLOCKS for up to `timeout_seconds` — callers own the
      budget. It is a waiting primitive, not a ceremony hot-path op; the handler
      is deliberately sync so the dispatch layer offloads it to a worker thread
      and the event loop stays live while it sleeps.

Idempotency (DEC-7 note, AC8 — platform risk was rated high): inherent — the loop
only reads (counts directory entries) and sleeps between checks; it performs no
writes, so an interrupted or repeated invocation just re-polls the same on-disk
state harmlessly.

Scope: `none` (manifest-justified) — `scratch_dir` is an explicit caller-supplied
param (same class as cartography.*'s target_root), not derived from
repo_root/_origin_worktree.

Negative-spec:
    - NEVER map a missing scratch_dir to `count: 0` — a caller waiting on a dir
      that was never created would otherwise burn its full timeout and report a
      clean "timeout" for what is actually a setup bug.
    - NEVER measure elapsed time with wall-clock (`time.time()` /
      `datetime.now()`) — DST/NTP steps would corrupt the deadline; monotonic
      only (CC-2).
    - No shell, no subprocess of any kind (CC-1 trivially satisfied): the bash
      oracle's `$SECONDS` loop is replaced wholesale, not wrapped.

Spec backlink: docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § B6
Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
    § DEC-2, DEC-7
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op


def _count_entries(scratch: Path):
    """Count direct entries of *scratch* via os.scandir.

    Returns (count, None) on success, (None, error_message) when the directory
    is missing or unreadable — a premise failure, never a zero count.
    """
    try:
        with os.scandir(scratch) as it:
            return len(list(it)), None
    except FileNotFoundError:
        return None, f"scratch_dir does not exist: {scratch}"
    except NotADirectoryError:
        return None, f"scratch_dir is not a directory: {scratch}"
    except OSError as exc:
        return None, f"scratch_dir is unreadable: {scratch}: {exc}"


@register_op("fanout.poll_scratch_dir")
def _poll_scratch_dir(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "fanout.poll_scratch_dir" handler (sync — offloaded to a thread).

    Required params: scratch_dir, min_count, timeout_seconds, poll_interval_seconds.

    BLOCKS for up to timeout_seconds (see module docstring — callers own the
    budget). Returns {status: "count_reached"|"timeout", count, elapsed_seconds},
    or {"error": <str>} on invalid params / missing scratch_dir.
    """
    scratch_dir = params.get("scratch_dir")
    if not scratch_dir or not isinstance(scratch_dir, str):
        return {"error": "fanout.poll_scratch_dir requires a non-empty scratch_dir param"}

    min_count = params.get("min_count")
    if not isinstance(min_count, int) or isinstance(min_count, bool) or min_count < 0:
        return {"error": "fanout.poll_scratch_dir requires min_count: a non-negative int"}

    timeout_seconds = params.get("timeout_seconds")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds < 0:
        return {"error": "fanout.poll_scratch_dir requires timeout_seconds: a non-negative number"}

    poll_interval_seconds = params.get("poll_interval_seconds")
    if (
        not isinstance(poll_interval_seconds, (int, float))
        or isinstance(poll_interval_seconds, bool)
        or poll_interval_seconds <= 0
    ):
        return {"error": "fanout.poll_scratch_dir requires poll_interval_seconds: a positive number"}

    scratch = Path(scratch_dir).expanduser()
    start = time.monotonic()
    deadline = start + float(timeout_seconds)

    while True:
        count, err = _count_entries(scratch)
        if err is not None:
            return {"error": err}
        now = time.monotonic()
        if count >= min_count:
            return {
                "status": "count_reached",
                "count": count,
                "elapsed_seconds": now - start,
            }
        if now >= deadline:
            return {
                "status": "timeout",
                "count": count,
                "elapsed_seconds": now - start,
            }
        time.sleep(float(poll_interval_seconds))
