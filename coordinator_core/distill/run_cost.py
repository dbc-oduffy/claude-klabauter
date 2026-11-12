"""Pure cost model of one distill-harvest run: agent dispatches and agent-read input bytes per phase.

Reads a consumer-shape workflow input (what `distill.workflow_input` emits) plus the files it
names; no LLM, no git, no network. Phases:

- load: one manifest-relay agent, plus one per batch file, plus one per wiki-slug shard.
- scan: one agent per batch.

Scan input bytes per agent = each batch file's bytes (its digest's bytes when the batch carries
``digests[file]``) + the bytes of every guide that agent is told to open (``relevantGuides`` when
the batch carries it, else every wiki guide). The guide term is an UPPER BOUND: it assumes every
listed guide is opened. Live telemetry is the real number; this model compares before/after.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from coordinator_core.ops.distill_workflow_input import _compact, shard_workflow_input

__all__ = ["project_run_cost", "scoped_files_sha256", "scoped_files"]


def scoped_files(workflow_input: dict[str, Any]) -> list[str]:
    """Every scoped file path across all batches, sorted and de-duplicated."""
    return sorted({f for batch in workflow_input["batches"] for f in batch["files"]})


def scoped_files_sha256(workflow_input: dict[str, Any]) -> str:
    """Order-independent identity of the scoped file set: sha256 over the sorted paths, newline-joined."""
    return hashlib.sha256("\n".join(scoped_files(workflow_input)).encode("utf-8")).hexdigest()


def _size(root: Path, rel: str, missing: list[str]) -> int:
    try:
        return (root / rel).stat().st_size
    except OSError:
        missing.append(rel)
        return 0


def _guide_paths(batch: dict[str, Any], all_guides: list[str]) -> list[str]:
    listed = batch.get("relevantGuides")
    if listed is None:
        return all_guides
    return sorted({g["path"] if isinstance(g, dict) else g for g in listed})


def project_run_cost(workflow_input: dict[str, Any], worktree_root: Path) -> dict[str, Any]:
    """Project dispatches and agent-read input bytes per phase for one run over `workflow_input`.

    Returns ``dispatches_by_phase`` {load, scan}, ``input_bytes_by_phase`` {load, scan},
    ``scan_bytes_split`` {files, guides}, ``scoped_files_sha256``, and ``unreadable_files`` (paths that did not stat, counted as 0 bytes).
    """
    root = Path(worktree_root)
    manifest, shards = shard_workflow_input(workflow_input)
    batch_files = manifest["batchFiles"]
    shard_files = manifest["slugShardFiles"]
    missing: list[str] = []

    load_bytes = len(_compact(manifest).encode("utf-8")) + sum(
        len(_compact(shards[name]).encode("utf-8")) for name in batch_files + shard_files
    )

    all_guides = sorted(set(workflow_input["wikiSlugs"].values()))
    guide_size: dict[str, int] = {}
    file_bytes = 0
    guide_bytes = 0
    for batch in workflow_input["batches"]:
        digests = batch.get("digests") or {}
        for rel in batch["files"]:
            file_bytes += _size(root, digests.get(rel, rel), missing)
        for guide in _guide_paths(batch, all_guides):
            if guide not in guide_size:
                guide_size[guide] = _size(root, guide, missing)
            guide_bytes += guide_size[guide]
    scan_bytes = file_bytes + guide_bytes

    return {
        "dispatches_by_phase": {
            "load": 1 + len(batch_files) + len(shard_files),
            "scan": len(workflow_input["batches"]),
        },
        "input_bytes_by_phase": {"load": load_bytes, "scan": scan_bytes},
        "scan_bytes_split": {"files": file_bytes, "guides": guide_bytes},
        "scoped_files_sha256": scoped_files_sha256(workflow_input),
        "unreadable_files": sorted(set(missing)),
    }
