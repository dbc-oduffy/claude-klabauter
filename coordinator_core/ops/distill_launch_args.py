"""
coordinator_core.ops.distill_launch_args — JSON-RPC "distill.launch_args" operation.

Purpose: hand a distill Workflow its whole input inline. The Workflow VM cannot read the
filesystem, so without this op each batch and wikislug shard is relayed through a load agent.
The op composes ``distill.workflow_input``'s payload (batches and wikiSlugs, validated) with the
curated-clusters file's path and sha256, and writes it through ``launch_args.build``, which
returns ``{path, sha256, args, args_bytes}``; the PreToolUse(Workflow) hook swaps the
``{argsFile, argsSha256}`` stub for the file's content.

Wire params:
    manifest (dict, REQUIRED), repo_root (str, REQUIRED), format_hints, backlog_run_id — as for
        distill.workflow_input.
    curated_file (str, optional) — repo-relative, confined to ``state/scratch/``; default
        ``state/scratch/artifact-distillation/<run_id>/curated.json`` (distill.write_curated_clusters).

Added args keys, only when that file exists (invocation A runs before curation and carries none):
``curatedTags`` (the file's ``verdicts`` list) plus the passthrough fields ``threshold_applied``,
``threshold_auto``, ``nugget_drop_share``, ``contradictionClusters`` and
``contradictionClusterCount`` (each copied when present). The file is never relayed by path.
A curated file that is not a JSON object with a ``verdicts`` list raises ValueError naming it.

Negative-spec: one write (``launch-args.json`` in the run dir); no git, no subprocess, no LLM call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coordinator_core.distill.launch_args import build
from coordinator_core.ipc import register_op
from coordinator_core.ops.distill_scope import RUN_DIR_REL
from coordinator_core.ops.distill_workflow_input import _handler as _workflow_input

__all__ = ["CURATED_TAGS_KEY", "curated_default_rel"]

CURATED_TAGS_KEY = "curatedTags"
_PASSTHROUGH_KEYS = (
    "threshold_applied",
    "threshold_auto",
    "nugget_drop_share",
    "contradictionClusters",
    "contradictionClusterCount",
)
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

    Returns ``{"path", "sha256", "args", "args_bytes"}`` as ``launch_args.build`` does: ``path`` is the
    written ``launch-args.json``, ``sha256`` is over its bytes, ``args`` is the inlined payload,
    ``args_bytes`` its size. Raises
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
        payload = {**payload, **_curated_args(curated, curated_rel)}
    return build(payload)


def _curated_args(curated: Path, rel: str) -> dict:
    try:
        data = json.loads(curated.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"distill.launch_args: curated file {rel} is unreadable or not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        raise ValueError(f"distill.launch_args: curated file {rel} must be a JSON object with a 'verdicts' list")
    return {CURATED_TAGS_KEY: data["verdicts"], **{k: data[k] for k in _PASSTHROUGH_KEYS if k in data}}
