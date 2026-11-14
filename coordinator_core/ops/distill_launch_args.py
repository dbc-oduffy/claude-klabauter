"""
coordinator_core.ops.distill_launch_args — JSON-RPC "distill.launch_args" operation.

Purpose: hand a distill Workflow its whole input inline. The Workflow VM cannot read the
filesystem, so without this op each batch and wikislug shard is relayed through a load agent.
The op composes ``distill.workflow_input``'s payload (batches and wikiSlugs, validated) with the
curated-clusters file's path and sha256, and writes it through ``launch_args.build``, which
returns ``{path, sha256, args}``; the PreToolUse(Workflow) hook swaps the
``{argsFile, argsSha256}`` stub for the file's content.

Wire params:
    manifest (dict, REQUIRED), repo_root (str, REQUIRED), format_hints, backlog_run_id — as for
        distill.workflow_input.
    curated_file (str, optional) — repo-relative, confined to ``state/scratch/``; default
        ``state/scratch/artifact-distillation/<run_id>/curated.json`` (distill.write_curated_clusters).

Added args keys: ``curatedFile`` (the repo-relative path) and ``curatedFileSha256`` (hex over the
file's bytes), only when that file exists; invocation A runs before curation and carries neither.

Negative-spec: one write (``launch-args.json`` in the run dir); no git, no subprocess, no LLM call.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from coordinator_core.distill.launch_args import build
from coordinator_core.ipc import register_op
from coordinator_core.ops.distill_scope import RUN_DIR_REL
from coordinator_core.ops.distill_workflow_input import _handler as _workflow_input

__all__ = ["CURATED_FILE_KEY", "CURATED_SHA_KEY", "curated_default_rel"]

CURATED_FILE_KEY = "curatedFile"
CURATED_SHA_KEY = "curatedFileSha256"
_SCRATCH_REL = "state/scratch"


def curated_default_rel(run_id: str) -> str:
    """Repo-relative default path of the curated-clusters file for `run_id`."""
    return f"{RUN_DIR_REL}/{run_id}/curated.json"


def _curated_target(root: Path, rel: str) -> Path:
    scratch = (root / _SCRATCH_REL).resolve()
    target = (root / rel).resolve()
    if target == scratch or scratch not in target.parents:
        raise ValueError(f"distill.launch_args: curated_file {rel!r} must be under {_SCRATCH_REL}/")
    return target


@register_op("distill.launch_args")
def _handler(params: dict, repo_root: Any = None) -> dict:
    """distill.launch_args handler.

    Returns ``{"path", "sha256", "args"}`` as ``launch_args.build`` does: ``path`` is the written
    ``launch-args.json``, ``sha256`` is over its bytes, ``args`` is the inlined payload. Raises
    ValueError on a missing manifest/repo_root or a curated_file outside ``state/scratch/``.
    The ``repo_root`` handler arg is unused; the param names the root, as in distill.workflow_input.
    """
    root_param = params.get("repo_root")
    if not root_param:
        raise ValueError("distill.launch_args requires a non-empty 'repo_root' param")
    root = Path(root_param)
    payload = _workflow_input(params, None)

    curated_rel = params.get("curated_file") or curated_default_rel(payload["runId"])
    if not isinstance(curated_rel, str):
        raise ValueError("distill.launch_args: curated_file must be a string")
    curated = _curated_target(root, curated_rel)
    if curated.is_file():
        payload = {
            **payload,
            CURATED_FILE_KEY: curated_rel,
            CURATED_SHA_KEY: hashlib.sha256(curated.read_bytes()).hexdigest(),
        }
    return build(payload)
