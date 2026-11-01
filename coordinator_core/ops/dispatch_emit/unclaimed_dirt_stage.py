"""Own-hunks-only blobs for baselined landing paths that hold unclaimed dirt.

Contract: ``own_only_blobs`` answers, per landing path with a recorded write baseline, whether the
worktree file is wholly the run's own (omitted: stage it as-is) or carries other hunks (a blob with
the run's hunks over HEAD, to stage index-only) or cannot be split (a refusal row). Zero spawns;
stdlib only at module top, readers imported lazily (cold-import pin).

Trap: paths with no baseline are omitted, the run's whole file is not distinguishable from dirt.
Trap: the blob is derived from worktree bytes at call time; an edit made before the commit that
consumes it is not carried.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple


def own_only_blobs(
    root: Path,
    run_id: str,
    paths: Iterable[str],
    baseline: Mapping[str, Optional[str]],
) -> Tuple[Dict[str, str], List[Tuple[str, Optional[str], str]]]:
    """``(blobs, refusals)``: ``blobs[path]`` is the sha of the worktree text with every non-own
    hunk reverted to HEAD; ``refusals`` is ``(path, run_or_None, header)`` for each hunk that
    shares lines with an own hunk, or ``(path, None, "unreadable")``. Never raises."""
    blobs: Dict[str, str] = {}
    refusals: List[Tuple[str, Optional[str], str]] = []
    root = Path(root)
    for path in paths:
        base = baseline.get(path)
        if base is None:
            continue
        try:
            from coordinator_core.ops.dispatch_emit import hunk_stage, kept_work

            got = hunk_stage.split(
                root, path, own_run_id=run_id, own_baseline_blob=base, foreign=[]
            )
            if got.overlaps or got.staged_blob is None:
                for run, header in got.overlaps or [(None, "unreadable")]:
                    refusals.append((path, run, header))
                continue
            form = kept_work.checkin_form(root, path, (root / path).read_bytes())
            if form is None:
                refusals.append((path, None, "unreadable"))
                continue
            if got.staged_blob == kept_work.blob_sha(form):
                continue
            blobs[path] = got.staged_blob
        except Exception:
            refusals.append((path, None, "unreadable"))
    return blobs, refusals
