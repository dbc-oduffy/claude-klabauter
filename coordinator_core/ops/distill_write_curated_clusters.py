"""
coordinator_core.ops.distill_write_curated_clusters — JSON-RPC
"distill.write_curated_clusters" operation.

Purpose: persist the curation for distill-harvest invocation B,
which would otherwise carry it inline in Workflow args (31-64KB). The curation itself
is ``distill.curate_clusters`` (compute-only); this op is the one write, kept apart so
that op stays pure.

Wire params:
    tag_counts (dict[str, int], REQUIRED), keep_threshold (int, optional) — as for
        distill.curate_clusters.
    output_path (str, REQUIRED) — repo-relative target, confined to
        ``state/scratch/``; written whole, atomically (create-or-full-rewrite).

File shape: the ``distill.curate_clusters`` return verbatim (``verdicts``, ``counts``,
``threshold_applied``, ``threshold_auto``, ``nugget_drop_share``, ``contradictionClusters``,
``degraded``) plus ``contradictionClusterCount`` — the shape distill-harvest's
``args.curatedFile`` loader reads.

Negative-spec: writes nothing outside ``output_path``; refuses a degraded curation
(empty or malformed tag_counts) rather than writing an empty verdict list.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.distill_curate_clusters import _AUTO, curate_clusters
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.wire_paths import rel_id

_SCRATCH_PREFIX = ("state", "scratch")


def curated_file_payload(result: dict) -> dict:
    return {**result, "contradictionClusterCount": len(result["contradictionClusters"])}


def _confined_target(worktree_root: Path, output_path: str) -> Path:
    scratch = (worktree_root.joinpath(*_SCRATCH_PREFIX)).resolve()
    target = (worktree_root / output_path).resolve()
    if target == scratch or scratch not in target.parents:
        raise ValueError(
            f"distill.write_curated_clusters: output_path {output_path!r} must be under "
            f"{'/'.join(_SCRATCH_PREFIX)}/"
        )
    return target


def _write_atomic(target: Path, obj: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, json.dumps(obj, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp_path, str(target))
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@register_op("distill.write_curated_clusters")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "distill.write_curated_clusters" handler.

    Returns ``{"output_path": <repo-relative path written>, "curated_verdict_count": int, "contradiction_cluster_count": int,
    "threshold_applied": int, "threshold_auto": bool}``. Raises ValueError on a
    missing repo_root, a missing/unconfined output_path, or a degraded curation.
    """
    if repo_root is None:
        raise ValueError(
            "distill.write_curated_clusters requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None."
        )
    output_path = params.get("output_path")
    if not isinstance(output_path, str) or not output_path:
        raise ValueError("distill.write_curated_clusters requires an output_path param")

    keep_threshold = params.get("keep_threshold", _AUTO)
    if keep_threshold is not _AUTO and (
        isinstance(keep_threshold, bool)
        or not isinstance(keep_threshold, int)
        or keep_threshold < 1
    ):
        keep_threshold = _AUTO

    worktree_root = main_worktree_root(repo_root)
    target = _confined_target(worktree_root, output_path)
    result = curate_clusters(params.get("tag_counts"), keep_threshold=keep_threshold)
    if result["degraded"]:
        raise ValueError(
            "distill.write_curated_clusters: tag_counts is missing, not a dict, or empty; "
            "nothing written"
        )
    payload = curated_file_payload(result)
    _write_atomic(target, payload)
    return {
        "output_path": rel_id(target, worktree_root),
        "curated_verdict_count": payload["counts"]["total"],
        "contradiction_cluster_count": payload["contradictionClusterCount"],
        "threshold_applied": result["threshold_applied"],
        "threshold_auto": result["threshold_auto"],
    }
