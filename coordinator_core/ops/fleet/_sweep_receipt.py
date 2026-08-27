"""
coordinator_core.ops.fleet._sweep_receipt — the artifact that makes a failed
archival sweep observable to a human.

AC-3 of state/handoffs/2026-08-25_roadmap-archival-sweeps-03.md: "the operator
learns that archival did not happen, by some artifact -- not by noticing a
cluttered directory later. Name the artifact." This module is that artifact.

THE FAILURE THIS EXISTS TO END. Every archival sweep on the ceremony's commit
path is non-fatal by construction: `commit_pipeline._run_in_plane_archive_sweep`
degrades a failed import, a contended lock, and any raise out of
`plan_sweep`/`apply_sweep` alike to `([], [])` plus a `_LOG.warning`, inside a
ceremony that then reports success. That is correct for the exit code and
useless to the operator -- it is the detached-and-silent shape the 2026-07-23
detached CLI fires were killed for, reproduced in-process. A warning nobody
reads is not observability.

WHERE IT LIVES, and why not under `state/`: `<common_dir>/coordinator-sessions/
archive-sweeps.receipt.jsonl`, the same git-common-dir-rooted location as the
cadence marker (`commit_pipeline._archive_sweep_marker`) and the single-flight
lock (`archive_terminal_handoffs._sweep_lock_path`), for that marker's own
load-bearing reason: these writes happen on the commit hot path, and a hot-path
write into the worktree leaves an untracked file behind every ceremony commit.
Under `.git/` the question does not arise, and a linked-worktree caller reads the
same file as the main worktree.

APPEND-ONLY, and that is the point. A receipt that is overwritten by the next
run answers "what happened last time" -- the operator needs "did it ever stop
working", which a success immediately following a failure would erase. Reading
is `tail`-shaped: the last line per `sweep` key is the current state.

Negative-spec:
  - NEVER raises. A sweep that cannot write its own receipt must not become a
    sweep that fails; every error degrades to a silent no-op, which is exactly
    the state the caller was already in before this module existed.
  - Does NOT decide anything. It records an outcome its caller already reached;
    it never classifies, retries, or gates.
  - Does NOT rotate or prune. Bounded by `_MAX_BYTES` instead: past that the
    file is truncated to its tail, because an unbounded append on a hot path is
    a disk leak and a truncated tail still answers the operator's question.

REMOVED 2026-08-27 (PM ruling, abd587695): the in-plane archival sweep
`commit_pipeline._run_in_plane_archive_sweep` and its three legs are GONE from the
commit path. Text below describing it is retained only as history of why this code
looks the way it does -- it asserts nothing about the commit path today. Handoffs are
archived at the occasions that create the work (pickup, workstream-complete,
workday-complete, and the per-artifact lifecycle paths), never by sweeping a corpus on
commit. See state/kill-ledger.md.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

__all__ = ["record_sweep_outcome", "receipt_path", "OUTCOMES"]

#: Closed set. A caller reaching for a sixth value is describing a distinction
#: the operator does not need -- fold it into `detail` instead.
OUTCOMES = ("applied", "nothing-to-do", "skipped-gated", "skipped-contended", "failed")

#: Tail retained when the file passes _MAX_BYTES. Small: the question is "is it
#: working now", and the answer is always in the last few lines per sweep.
_MAX_BYTES = 256 * 1024
_KEEP_BYTES = 64 * 1024


def receipt_path(common_dir: Path) -> Path:
    """The receipt file for this checkout. See module docstring for siting."""
    return common_dir / "coordinator-sessions" / "archive-sweeps.receipt.jsonl"


def record_sweep_outcome(
    common_dir: Optional[Path],
    sweep: str,
    outcome: str,
    *,
    count: int = 0,
    detail: Optional[str] = None,
) -> None:
    """Append one sweep outcome. Never raises -- see module negative-spec.

    `sweep` is the op key the outcome belongs to (e.g.
    "fleet.archive_completed_plans"), so one file serves every sweep and the
    operator reads one place rather than three.

    `detail` carries the reason a `failed` or `skipped-*` outcome happened, in
    whatever words the caller already has. It is free text by design: the
    caller's own exception string is more use to a human than a re-coded enum.
    """
    if common_dir is None or outcome not in OUTCOMES:
        return
    try:
        path = receipt_path(Path(common_dir))
        path.parent.mkdir(parents=True, exist_ok=True)

        row = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sweep": sweep,
            "outcome": outcome,
            "count": count,
        }
        if detail:
            # Bounded: a caller handing over a full traceback must not turn one
            # receipt line into the whole file's byte budget.
            row["detail"] = detail[:512]

        line = json.dumps(row, sort_keys=True) + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)

        _truncate_if_oversized(path)
    except Exception:  # noqa: BLE001 -- see module negative-spec
        return


def _truncate_if_oversized(path: Path) -> None:
    """Keep the tail, drop the head, only once past `_MAX_BYTES`.

    Rewrites through a sibling temp + `os.replace` so a concurrent reader sees
    either the old file or the new one, never a half-truncated one. Line-aligned:
    the first (probably partial) line of the retained tail is discarded so every
    surviving line parses.
    """
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return
        with open(path, "rb") as handle:
            handle.seek(-_KEEP_BYTES, os.SEEK_END)
            tail = handle.read()
        newline = tail.find(b"\n")
        tail = tail[newline + 1 :] if newline != -1 else b""
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(tail)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 -- see module negative-spec
        return
