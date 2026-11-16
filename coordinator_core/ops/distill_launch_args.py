"""
coordinator_core.ops.distill_launch_args — JSON-RPC "distill.launch_args" operation.

Purpose: hand a distill Workflow its whole input inline. The Workflow VM cannot read the
filesystem, so without this op each batch and wikislug shard is relayed through a load agent.
The op composes ``distill.workflow_input``'s payload (batches and wikiSlugs, validated) with the
curated-clusters file's path and sha256, and writes it through ``launch_args.build``, which
returns ``{path, sha256, args, args_bytes}``; the PreToolUse(Workflow) hook swaps the
``{argsFile, argsSha256}`` stub for the file's content.

Wire params:
    manifest (dict, REQUIRED) — the scope manifest (input.json), or the distill.scope result, whose
        ``input_json_path`` (repo-relative, under ``state/scratch/``) is loaded; any other shape
        raises ValueError naming the wanted keys.
    repo_root (str, REQUIRED), format_hints, backlog_run_id — as for
        distill.workflow_input.
    curated_file (str, optional) — repo-relative, confined to ``state/scratch/``; default
        ``state/scratch/artifact-distillation/<run_id>/curated.json`` (distill.write_curated_clusters).

Added args keys, only when that file exists (invocation A runs before curation and carries none):
``curatedTags`` (the file's ``verdicts`` list) plus the passthrough fields ``threshold_applied``,
``threshold_auto``, ``nugget_drop_share``, ``contradictionClusters`` and
``contradictionClusterCount`` (each copied when present). The file is never relayed by path.
A curated file that is not a JSON object with a ``verdicts`` list raises ValueError naming it.

    prepared_path (str, optional) — repo-relative, confined to ``state/scratch/``: the ``prepared.json``
        that ``distill_mechanical.py prepare`` wrote. Its object is inlined as ``args.prepared``, the input
        distill-harvest reads; a missing file or one that is not a JSON object raises ValueError naming it.

Leg 2 (``leg: 2``, the resume after ``distill_mechanical.py finalize``): params ``run_id`` (REQUIRED),
``repo_root``, ``promoted_path`` and ``splice_report_path`` (both REQUIRED, confined to ``state/scratch/``:
finalize-report ``phases.promote.promoted``, a list, and ``phases["synth-splice"]``, an object). Leg 1's
``launch-args.json`` in that run dir is reused verbatim, with ``promoted`` and ``spliceReport`` added,
and written as ``launch-args-leg2.json``. Leg 1's file is never rewritten, because rebuilding it from
params could drift from what leg 1 actually ran on. The reply shape is the same; the Workflow also needs
``resumeFromRunId``.

Negative-spec: one write (``launch-args.json`` in the run dir); no git, no subprocess, no LLM call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coordinator_core.distill.launch_args import ARGS_FILE_NAME, build, unchunk
from coordinator_core.ipc import register_op
from coordinator_core.ops.distill_scope import RUN_DIR_REL
from coordinator_core.ops.distill_workflow_input import _handler as _workflow_input

__all__ = ["CURATED_TAGS_KEY", "PREPARED_KEY", "curated_default_rel"]

CURATED_TAGS_KEY = "curatedTags"
PREPARED_KEY = "prepared"
LEG2_FILE_NAME = "launch-args-leg2.json"
_PASSTHROUGH_KEYS = (
    "threshold_applied",
    "threshold_auto",
    "nugget_drop_share",
    "contradictionClusters",
    "contradictionClusterCount",
)
_SCRATCH_REL = "state/scratch"
_MANIFEST_KEYS = frozenset({"run_id", "batches", "wiki_dirs", "wiki_slugs"})


def curated_default_rel(run_id: str) -> str:
    """Repo-relative default path of the curated-clusters file for `run_id`."""
    return f"{RUN_DIR_REL}/{run_id}/curated.json"


def _scratch_target(root: Path, rel: str, label: str) -> Path:
    scratch = (root / _SCRATCH_REL).resolve()
    target = (root / rel).resolve()
    if target == scratch or scratch not in target.parents:
        raise ValueError(f"distill.launch_args: {label} {rel!r} must be under {_SCRATCH_REL}/")
    return target


def _curated_target(root: Path, rel: str) -> Path:
    return _scratch_target(root, rel, "curated_file")


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
    if params.get("leg") is not None:
        return _leg2(params, root)
    payload = _workflow_input(_with_scope_manifest(params, root), None)

    curated_rel = params.get("curated_file") or curated_default_rel(payload["runId"])
    if not isinstance(curated_rel, str):
        raise ValueError("distill.launch_args: curated_file must be a string")
    curated = _curated_target(root, curated_rel)
    if curated.is_file():
        payload = {**payload, **_curated_args(curated, curated_rel)}
    prepared_rel = params.get("prepared_path")
    if prepared_rel is not None:
        payload = {**payload, PREPARED_KEY: _prepared(root, prepared_rel)}
    return build(payload)


def _leg2(params: dict, root: Path) -> dict:
    if params.get("leg") != 2:
        raise ValueError("distill.launch_args: leg must be 2 (omit it for leg 1)")
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("distill.launch_args: leg 2 requires 'run_id'")
    run_dir = _scratch_target(root, f"{RUN_DIR_REL}/{run_id}", "run_id")
    leg1 = run_dir / ARGS_FILE_NAME
    try:
        args = json.loads(leg1.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"distill.launch_args: leg 2 needs leg 1's {RUN_DIR_REL}/{run_id}/{ARGS_FILE_NAME}: {exc}") from exc
    promoted = _scratch_json(root, params.get("promoted_path"), "promoted_path")
    splice = _scratch_json(root, params.get("splice_report_path"), "splice_report_path")
    if not isinstance(promoted, list):
        raise ValueError("distill.launch_args: promoted_path must hold a JSON list (finalize-report phases.promote.promoted)")
    if not isinstance(splice, dict):
        raise ValueError('distill.launch_args: splice_report_path must hold a JSON object (finalize-report phases["synth-splice"])')
    payload = {k: v for k, v in unchunk(args).items() if k != "argsInlined"}
    return build({**payload, "promoted": promoted, "spliceReport": splice}, run_dir=run_dir, file_name=LEG2_FILE_NAME)


def _scratch_json(root: Path, rel: Any, label: str) -> Any:
    if not isinstance(rel, str) or not rel:
        raise ValueError(f"distill.launch_args: leg 2 requires '{label}'")
    try:
        return json.loads(_scratch_target(root, rel, label).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"distill.launch_args: {label} {rel} is unreadable or not JSON: {exc}") from exc


def _prepared(root: Path, rel: Any) -> dict:
    if not isinstance(rel, str) or not rel:
        raise ValueError("distill.launch_args: prepared_path must be a non-empty string")
    target = _scratch_target(root, rel, "prepared_path")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"distill.launch_args: prepared file {rel} is unreadable or not JSON: {exc}; "
                         "distill_mechanical.py prepare writes it") from exc
    if not isinstance(data, dict):
        raise ValueError(f"distill.launch_args: prepared file {rel} must be a JSON object (prepared.json)")
    return data


def _with_scope_manifest(params: dict, root: Path) -> dict:
    manifest = params.get("manifest")
    run_id = params.get("run_id")
    if manifest is None and isinstance(run_id, str) and run_id:
        # distill.scope always writes input.json at this path, so run_id alone names the manifest.
        manifest = {"input_json_path": f"{RUN_DIR_REL}/{run_id}/input.json"}
    if not isinstance(manifest, dict):
        raise ValueError(
            "distill.launch_args requires a 'manifest' param: the distill.scope result "
            "(or the input.json it points at, which distill.scope writes)"
        )
    if "input_json_path" in manifest and not _MANIFEST_KEYS <= manifest.keys():
        rel = manifest["input_json_path"]
        if not isinstance(rel, str):
            raise ValueError("distill.launch_args: manifest.input_json_path must be a string")
        target = _scratch_target(root, rel, "manifest.input_json_path")
        try:
            manifest = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"distill.launch_args: scope manifest {rel} is unreadable or not JSON: {exc}") from exc
    if not isinstance(manifest, dict) or not _MANIFEST_KEYS <= manifest.keys():
        raise ValueError(
            f"distill.launch_args: manifest must be the scope manifest (input.json) with keys {sorted(_MANIFEST_KEYS)}, "
            "or the distill.scope result carrying its 'input_json_path'"
        )
    return {**params, "manifest": manifest}


def _curated_args(curated: Path, rel: str) -> dict:
    try:
        data = json.loads(curated.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"distill.launch_args: curated file {rel} is unreadable or not JSON: {exc}; "
                         "distill.write_curated_clusters writes it") from exc
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        raise ValueError(f"distill.launch_args: curated file {rel} must be a JSON object with a 'verdicts' list "
                         "(as written by distill.write_curated_clusters)")
    return {CURATED_TAGS_KEY: data["verdicts"], **{k: data[k] for k in _PASSTHROUGH_KEYS if k in data}}
