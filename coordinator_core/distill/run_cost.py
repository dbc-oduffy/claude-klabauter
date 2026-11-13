"""Pure cost model of one distill-harvest run: agent dispatches and agent-read input bytes per phase.

Reads a consumer-shape workflow input (what `distill.workflow_input` emits) plus the files it
names; no LLM, no git, no network. Phases:

- load: one manifest-relay agent, plus one per batch file, plus one per wiki-slug shard; 0 when the
  input carries ``argsInlined`` (engine-inlined Workflow args, no relay agent).
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

from coordinator_core.distill.launch_args import INLINED_KEY, unchunk
from coordinator_core.distill.prompt_tier import estimate_tokens
from coordinator_core.ops.distill_workflow_input import _compact, guide_paths, shard_workflow_input

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


def project_run_cost(workflow_input: dict[str, Any], worktree_root: Path) -> dict[str, Any]:
    """Project dispatches and agent-read input bytes per phase for one run over `workflow_input`.

    Returns ``dispatches_by_phase`` {load, scan}, ``input_bytes_by_phase`` {load, scan},
    ``scan_bytes_split`` {files, guides}, ``scan_prompt_tokens_by_batch`` (bytes/4 per scan agent,
    in batch order), ``max_scan_prompt_tokens``, ``scoped_files_sha256``, and ``unreadable_files`` (paths that did not stat, counted as 0 bytes).
    """
    root = Path(worktree_root)
    inlined = bool(workflow_input.get(INLINED_KEY))
    workflow_input = unchunk(workflow_input)
    manifest, shards = shard_workflow_input(workflow_input)
    batch_files = manifest["batchFiles"]
    shard_files = manifest["slugShardFiles"]
    missing: list[str] = []

    load_bytes = 0 if inlined else len(_compact(manifest).encode("utf-8")) + sum(
        len(_compact(shards[name]).encode("utf-8")) for name in batch_files + shard_files
    )

    all_guides = sorted(set(workflow_input["wikiSlugs"].values()))
    guide_size: dict[str, int] = {}
    file_bytes = 0
    guide_bytes = 0
    prompt_tokens: list[int] = []
    for batch in workflow_input["batches"]:
        digests = batch.get("digests") or {}
        batch_file_bytes = sum(_size(root, digests.get(rel, rel), missing) for rel in batch["files"])
        batch_guide_bytes = 0
        for guide in guide_paths(batch, all_guides):
            if guide not in guide_size:
                guide_size[guide] = _size(root, guide, missing)
            batch_guide_bytes += guide_size[guide]
        file_bytes += batch_file_bytes
        guide_bytes += batch_guide_bytes
        prompt_tokens.append(estimate_tokens(batch_file_bytes + batch_guide_bytes))
    scan_bytes = file_bytes + guide_bytes

    return {
        "dispatches_by_phase": {
            "load": 0 if inlined else 1 + len(batch_files) + len(shard_files),
            "scan": len(workflow_input["batches"]),
        },
        "input_bytes_by_phase": {"load": load_bytes, "scan": scan_bytes},
        "scan_bytes_split": {"files": file_bytes, "guides": guide_bytes},
        "scan_prompt_tokens_by_batch": prompt_tokens,
        "max_scan_prompt_tokens": max(prompt_tokens, default=0),
        "scoped_files_sha256": scoped_files_sha256(workflow_input),
        "unreadable_files": sorted(set(missing)),
    }
