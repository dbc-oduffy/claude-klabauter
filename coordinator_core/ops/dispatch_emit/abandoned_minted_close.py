"""Close dead `abandoned_at_minted` halt markers so they stop reading as open halts.

A run the stale-minted reaper halted `abandoned_at_minted` was a shell (nothing withheld, no live run
token). Its marker stays `halted` forever unless closed; `close_abandoned` rewrites it `closed` through `halted_run.mark_closed`, except while a descendant run is active (`_descendant_activity_epoch`).
Stat and marker reads only: no git, no subprocess. At most `limit` markers are closed per call, and the scan stops at `SCAN_BUDGET_SECONDS`.

`python -m coordinator_core.ops.dispatch_emit.abandoned_minted_close --root <repo>` closes one bounded batch.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Optional, Sequence

from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import halted_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, record_dir
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.warp_run_deliverable import (
    ABANDONED_AT_MINTED,
    MINTED_STALE_SECONDS,
    _descendant_activity_epoch,
)

CLOSE_REASON = "abandoned_at_minted: shell run with nothing withheld and no live descendant; closed"
DEFAULT_LIMIT = 8
SCAN_BUDGET_SECONDS = 0.2


def _closable(wt: Path, run_id: str, marker: dict, now: float) -> bool:
    """A halted `abandoned_at_minted` marker (the caller filtered on both) with nothing withheld, no live
    token and no active descendant."""
    if halted_run._withheld_of(marker):
        return False
    if run_token.is_live(wt, run_id):
        return False
    child = _descendant_activity_epoch(wt, run_id, now)
    return child is None or now - child > MINTED_STALE_SECONDS


def close_abandoned(
    root: Any, now: Optional[float] = None, limit: int = DEFAULT_LIMIT, skip: Collection[str] = (),
) -> list[str]:
    """Close up to `limit` dead `abandoned_at_minted` markers (oldest run id first); returns the closed run ids.
    Only closes count against `limit`; a kept (not closable) marker is bounded by `SCAN_BUDGET_SECONDS` instead,
    so unclosable markers at the oldest end never starve the closable ones behind them.
    Run ids in `skip` are never touched (the caller just halted them)."""
    wt = main_worktree_root(Path(root))
    current = time.time() if now is None else now
    deadline = time.process_time() + SCAN_BUDGET_SECONDS
    at = datetime.fromtimestamp(current, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        names = sorted(e.name for e in os.scandir(wt / RECORD_DIR_ROOT) if e.is_dir())
    except OSError:
        return []
    closed: list[str] = []
    for name in names:
        if len(closed) >= limit or time.process_time() > deadline:
            break
        if name in skip or not safe_id(name):
            continue
        rdir = record_dir(wt, name)
        marker = halted_run._read(rdir)
        if marker is None or marker.get("halted") != ABANDONED_AT_MINTED or marker.get("status") != "halted":
            continue
        if _closable(wt, name, marker, current) and halted_run.mark_closed(rdir, CLOSE_REASON, at):
            closed.append(name)
    return closed


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="abandoned_minted_close", description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repo root (default: the working directory)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)
    closed = close_abandoned(Path(args.root).resolve(), limit=args.limit)
    print(json.dumps({"closed": closed}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
