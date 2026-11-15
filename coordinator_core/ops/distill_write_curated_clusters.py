"""
coordinator_core.ops.distill_write_curated_clusters — JSON-RPC
"distill.write_curated_clusters" operation.

Purpose: persist the curation for distill-harvest invocation B (31-64KB). The curation itself
is ``distill.curate_clusters`` (compute-only); this op is the one write, kept apart so
that op stays pure. ``distill.launch_args`` reads the file and inlines it as ``args.curatedTags``.

Wire params:
    tag_counts (dict[str, int]), keep_threshold (int, optional) — as for
        distill.curate_clusters. When ``tag_counts`` is absent it is derived from invocation
        A's result, taken from the first of: ``invocation_a`` (the Workflow result verbatim,
        a dict), ``invocation_a_file`` (repo-relative JSON of that result, confined to
        ``state/scratch/``), or ``state/scratch/artifact-distillation/<run_id>/invocation-a.json``
        when ``run_id`` is given and the file exists. A derived ``recommended_keep_threshold``
        applies when ``keep_threshold`` is not supplied.
    output_path (str, optional) — repo-relative target, confined to
        ``state/scratch/``; written whole, atomically (create-or-full-rewrite).
    run_id (str, optional) — when ``output_path`` is absent, the target defaults to
        ``state/scratch/artifact-distillation/<run_id>/curated.json``. One of the two
        is required.

The target repo's top-level ``docs/wiki`` page stems and folder names are passed to the curation
as ``wiki_names``, so a bare tag with an existing wiki home is kept.

File shape: the ``distill.curate_clusters`` return verbatim (``verdicts``, ``counts``,
``threshold_applied``, ``threshold_auto``, ``nugget_drop_share``, ``contradictionClusters``,
``degraded``) plus ``contradictionClusterCount`` — the shape ``distill.launch_args`` reads.

Negative-spec: writes nothing outside ``output_path``; refuses a degraded curation
(empty or malformed tag_counts) rather than writing an empty verdict list.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.distill_curate_clusters import _AUTO, curate_clusters
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.wire_paths import rel_id

_SCRATCH_PREFIX = ("state", "scratch")
_DEFAULT_DIR = (*_SCRATCH_PREFIX, "artifact-distillation")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


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


def _wiki_names(worktree_root: Path) -> list:
    """Top-level docs/wiki page stems and folder names; one directory listing, no recursion."""
    try:
        with os.scandir(worktree_root.joinpath("docs", "wiki")) as entries:
            return sorted(
                Path(e.name).stem if e.is_file() else e.name
                for e in entries
                if e.is_dir() or e.name.endswith(".md")
            )
    except OSError:
        return []


_INVOCATION_A_NAME = "invocation-a.json"


def _read_invocation_a(worktree_root: Path, params: dict, run_id: object) -> Optional[dict]:
    """Invocation A's Workflow result from the params, or None when none is reachable."""
    inline = params.get("invocation_a")
    if inline is not None:
        if not isinstance(inline, dict):
            raise ValueError("distill.write_curated_clusters: invocation_a must be an object")
        return inline
    rel = params.get("invocation_a_file")
    if rel:
        if not isinstance(rel, str):
            raise ValueError("distill.write_curated_clusters: invocation_a_file must be a string")
        path = _confined_target(worktree_root, rel)
    elif isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id):
        path = worktree_root.joinpath(*_DEFAULT_DIR, run_id, _INVOCATION_A_NAME)
    else:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if rel:
            raise ValueError(f"distill.write_curated_clusters: invocation_a_file {rel!r} not found")
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"distill.write_curated_clusters: unreadable invocation A result {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("distill.write_curated_clusters: invocation A result must be a JSON object")
    return data


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
    if output_path is None or output_path == "":
        run_id = params.get("run_id")
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(
                "distill.write_curated_clusters requires output_path, or run_id "
                "(the distill.scope result's run_id; letters, digits, '.', '_', '-') to default it to "
                f"{'/'.join(_DEFAULT_DIR)}/<run_id>/curated.json"
            )
        output_path = "/".join((*_DEFAULT_DIR, run_id, "curated.json"))
    elif not isinstance(output_path, str):
        raise ValueError("distill.write_curated_clusters: output_path must be a string")

    tag_counts = params.get("tag_counts")
    keep_threshold = params.get("keep_threshold", _AUTO)
    if tag_counts is None:
        derived = _read_invocation_a(main_worktree_root(repo_root), params, params.get("run_id"))
        if derived is not None:
            tag_counts = derived.get("tag_counts")
            if "keep_threshold" not in params:
                keep_threshold = derived.get("recommended_keep_threshold", _AUTO)
    if keep_threshold is not _AUTO and (
        isinstance(keep_threshold, bool)
        or not isinstance(keep_threshold, int)
        or keep_threshold < 1
    ):
        keep_threshold = _AUTO

    worktree_root = main_worktree_root(repo_root)
    target = _confined_target(worktree_root, output_path)
    result = curate_clusters(
        tag_counts, keep_threshold=keep_threshold, wiki_names=_wiki_names(worktree_root)
    )
    if result["degraded"]:
        raise ValueError(
            "distill.write_curated_clusters: tag_counts is missing, not a dict, or empty; "
            "it is the tag_counts of invocation A's result (the distill Workflow run launched with "
            "distill.launch_args args; pass invocation_a / invocation_a_file or tag_counts); nothing written"
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
