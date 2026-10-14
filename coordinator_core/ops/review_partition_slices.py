"""
coordinator_core.ops.review_partition_slices — registers ``review.partition_slices``:
the brightline verdict plus, when it is PARTITION-MANDATORY, the run's product files
grouped into at most six review slices, each frozen in-process through
``review_freeze_diff.freeze_diff``: committed ``base..head`` in range mode (``head`` given),
``base``->worktree otherwise.

Grouping rule: the first two DIRECTORY segments of a product path (``a/b/c.py`` -> ``a/b``;
a top-level file -> ``.``); while more than ``MAX_SLICES`` groups remain, the two smallest
(file count, then key) merge. Slice ids are ``<slice_prefix>-<n>``, n = 1.. in key order.

Negative-spec: a ``single-reviewer-ok`` verdict returns ``slices: []`` -- no partition; the
caller reviews the whole diff as one slice. Exactly one of ``head`` / ``worktree=true``: both
or neither is a structured error. Slices carry different pathspecs, which
``freeze_diffs_batch`` refuses to mix, so each slice is its own freeze call.
"""

from __future__ import annotations

MUTATES = ["state/review-trail/diffs/*.diff", "state/review-trail/diffs/*.head.sha"]

from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.review_brightline_gate import (
    RangeMeasureError,
    _verdict,
    range_measure,
    worktree_measure,
)
from coordinator_core.ops.review_freeze_diff import freeze_diff

MAX_SLICES = 6


def _group_key(path: str) -> str:
    return "/".join(path.split("/")[:-1][:2]) or "."


def group_paths(paths: List[str], max_slices: int = MAX_SLICES) -> List[List[str]]:
    """Group `paths` by `_group_key`, merge to at most `max_slices` groups; deterministic."""
    groups: Dict[str, List[str]] = {}
    for p in sorted(set(paths)):
        groups.setdefault(_group_key(p), []).append(p)
    ordered = [(key, files) for key, files in sorted(groups.items())]
    while len(ordered) > max_slices:
        by_size = sorted(range(len(ordered)), key=lambda i: (len(ordered[i][1]), ordered[i][0]))
        a, b = sorted(by_size[:2])
        merged = (ordered[a][0], sorted(ordered[a][1] + ordered[b][1]))
        ordered = [g for i, g in enumerate(ordered) if i not in (a, b)] + [merged]
        ordered.sort(key=lambda g: g[0])
    return [files for _, files in ordered]


def partition_slices(
    repo_root: Path, base: str, paths: List[str], slice_prefix: str, *, head: Optional[str] = None
) -> dict:
    """Gate verdict over `paths`, and the frozen slices when partitioned.

    With `head` set, measures and freezes committed `base..head` only (range mode); otherwise
    `base`->worktree. Range mode never reads the working tree.

    Returns `{verdict, loc, surfaces, product_paths: [..], slices: [{slice_id, paths, diff_path}],
    error}`; `error` is None on success. A freeze refusal is returned as `error` with the
    slices frozen so far, never raised.
    """
    out: dict = {"verdict": None, "loc": 0, "surfaces": 0, "product_paths": [], "slices": [], "error": None}
    if not base or not paths or not slice_prefix:
        out["error"] = "base, paths and slice_prefix are required"
        return out
    if head:
        try:
            loc, surfaces, files = range_measure(base, head, paths, str(repo_root))
        except RangeMeasureError as exc:
            out["error"] = str(exc)
            return out
    else:
        loc, surfaces, files = worktree_measure(base, paths, str(repo_root))
    verdict = _verdict(loc, 0, len(surfaces))
    out.update(verdict=verdict, loc=loc, surfaces=len(surfaces), product_paths=sorted(files))
    if verdict != "PARTITION-MANDATORY" or not files:
        return out
    for n, group in enumerate(group_paths(sorted(files)), 1):
        slice_id = f"{slice_prefix}-{n}"
        if head:
            frozen = freeze_diff(repo_root, f"{base}..{head}", slice_id, group)
        else:
            frozen = freeze_diff(repo_root, base, slice_id, group, worktree=True)
        if frozen.get("error"):
            out["error"] = f"freeze of {slice_id} failed: {frozen['error']}"
            return out
        out["slices"].append({"slice_id": slice_id, "paths": group, "diff_path": frozen["diff_path"]})
    return out


@register_op("review.partition_slices")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "review.partition_slices". Params: ``base`` (str), ``paths`` (list[str]),
    ``slice_prefix`` (str), and exactly one of ``head`` (str, committed range mode) or
    ``worktree: true``. Keying scope: show_top."""
    if repo_root is None:
        return {"error": "review.partition_slices requires a show_top-keyed dispatch; repo_root was not supplied"}
    paths = params.get("paths")
    if not isinstance(paths, list):
        return {"error": "review.partition_slices requires params.paths as a list"}
    head = str(params.get("head") or "")
    worktree = bool(params.get("worktree"))
    if bool(head) == worktree:
        return {"error": "review.partition_slices requires exactly one of head (range mode) or worktree=true"}
    return partition_slices(
        repo_root,
        str(params.get("base") or ""),
        [str(p) for p in paths],
        str(params.get("slice_prefix") or ""),
        head=head or None,
    )
