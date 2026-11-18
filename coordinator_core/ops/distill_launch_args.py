"""
coordinator_core.ops.distill_launch_args — JSON-RPC "distill.launch_args" operation.

Purpose: hand a distill Workflow its whole input without the payload crossing the EM context or the
Workflow arg cap. The op composes ``distill.workflow_input``'s payload (batches and wikiSlugs,
validated), writes it through ``launch_args.build`` (``launch-args.json``), then writes a payload-only
launcher (``launch_script.write_launcher``) that nests the harvest script by scriptPath. The reply is
``{scriptPath, sha256, launcher_bytes, args_path, args_sha256}``, no ``args``; the EM fires
``Workflow({scriptPath})``.

Wire params:
    workflow_script (str, REQUIRED) — absolute path of an existing distill-harvest script, nested by the launcher.
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
        distill-harvest reads; when it carries ``wikiSlugsPacked`` the top-level ``wikiSlugs`` is dropped
        (one slug index in the args); a missing file or one that is not a JSON object raises ValueError naming it.

Leg 2 (``leg: 2``, the resume after ``distill_mechanical.py finalize``): params ``run_id`` (REQUIRED),
``repo_root``, ``promoted_path`` and ``splice_report_path`` (both REQUIRED, confined to ``state/scratch/``:
finalize-report ``phases.promote.promoted``, a list, and ``phases["synth-splice"]``, an object). Leg 1's
``launch-args.json`` in that run dir is reused verbatim, with ``promoted`` and ``spliceReport`` added,
and written as ``launch-args-leg2.json``. Leg 1's file is never rewritten, because rebuilding it from
params could drift from what leg 1 actually ran on. The reply shape is the same, from ``launch-leg2.workflow.mjs``;
the Workflow also needs ``resumeFromRunId``.

Sharding: when ``prepared_path`` is ``prepared-NN.json`` (or ``shard`` names NN, accepted on both legs),
every file name carries ``-NN`` (``launch-NN.workflow.mjs``, ``launch-args-NN.json``, ``launch-leg2-NN.workflow.mjs``,
``launch-args-leg2-NN.json``), so shards never overwrite each other. Leg 2 reads that shard's leg-1 args file;
pass the shard's ``prepared_path`` or ``shard`` on leg 2.

The launcher is written before the args file, so a launcher over the cap raises with neither on disk.

Negative-spec: writes only in the run dir (args file, launcher, receipt); no git, no subprocess, no LLM call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from coordinator_core.distill.launch_args import ARGS_FILE_NAME, INLINED_KEY, build, chunk_arrays, unchunk
from coordinator_core.distill.launch_script import LAUNCHER_FILE_NAME, write_launcher
from coordinator_core.ipc import register_op
from coordinator_core.ops.distill_scope import RUN_DIR_REL
from coordinator_core.ops.distill_workflow_input import _handler as _workflow_input

__all__ = ["CURATED_TAGS_KEY", "PREPARED_KEY", "curated_default_rel"]

CURATED_TAGS_KEY = "curatedTags"
PREPARED_KEY = "prepared"
PACKED_SLUGS_KEY = "wikiSlugsPacked"
LEG2_FILE_NAME = "launch-args-leg2.json"
LEG2_LAUNCHER_NAME = "launch-leg2.workflow.mjs"
_PASSTHROUGH_KEYS = (
    "threshold_applied",
    "threshold_auto",
    "nugget_drop_share",
    "contradictionClusters",
    "contradictionClusterCount",
)
_SHARD_RE = re.compile(r"prepared-([0-9]+)\.json")
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

    Returns ``{"scriptPath", "sha256", "launcher_bytes", "args_path", "args_sha256"}`` with no ``args``:
    ``scriptPath`` is the written launcher (sha256 over its bytes), ``args_path`` the ``launch-args.json``
    beside it. Raises ValueError on a missing manifest/repo_root/workflow_script or a curated_file
    outside ``state/scratch/``.
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
        prepared = _prepared(root, prepared_rel)
        if PACKED_SLUGS_KEY in prepared:
            payload = {k: v for k, v in payload.items() if k != "wikiSlugs"}
        payload = {**payload, PREPARED_KEY: prepared}
    suffix = _shard_suffix(params)
    return _launch(
        payload,
        _workflow_script(params),
        _sharded(LAUNCHER_FILE_NAME, suffix),
        file_name=_sharded(ARGS_FILE_NAME, suffix),
    )


def _shard_suffix(params: dict) -> str:
    """``-NN`` when ``shard`` or a ``prepared-NN.json`` ``prepared_path`` names a shard, else ``""``."""
    shard = params.get("shard")
    if shard is not None:
        if isinstance(shard, bool) or not isinstance(shard, (int, str)) or not re.fullmatch(r"[0-9]+", str(shard)):
            raise ValueError("distill.launch_args: shard must be a non-negative integer or digit string")
        return f"-{int(shard):02d}"
    rel = params.get("prepared_path")
    match = _SHARD_RE.fullmatch(Path(rel).name) if isinstance(rel, str) else None
    return f"-{int(match.group(1)):02d}" if match else ""


def _sharded(name: str, suffix: str) -> str:
    stem, dot, ext = name.partition(".")
    return f"{stem}{suffix}{dot}{ext}"


def _leg2(params: dict, root: Path) -> dict:
    if params.get("leg") != 2:
        raise ValueError("distill.launch_args: leg must be 2 (omit it for leg 1)")
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("distill.launch_args: leg 2 requires 'run_id'")
    script = _workflow_script(params)
    run_dir = _scratch_target(root, f"{RUN_DIR_REL}/{run_id}", "run_id")
    suffix = _shard_suffix(params)
    leg1 = run_dir / _sharded(ARGS_FILE_NAME, suffix)
    try:
        args = json.loads(leg1.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"distill.launch_args: leg 2 needs leg 1's {RUN_DIR_REL}/{run_id}/{leg1.name}: {exc}") from exc
    promoted = _scratch_json(root, params.get("promoted_path"), "promoted_path")
    splice = _scratch_json(root, params.get("splice_report_path"), "splice_report_path")
    if not isinstance(promoted, list):
        raise ValueError("distill.launch_args: promoted_path must hold a JSON list (finalize-report phases.promote.promoted)")
    if not isinstance(splice, dict):
        raise ValueError('distill.launch_args: splice_report_path must hold a JSON object (finalize-report phases["synth-splice"])')
    payload = {k: v for k, v in unchunk(args).items() if k != "argsInlined"}
    return _launch(
        {**payload, "promoted": promoted, "spliceReport": splice},
        script,
        _sharded(LEG2_LAUNCHER_NAME, suffix),
        run_dir=run_dir,
        file_name=_sharded(LEG2_FILE_NAME, suffix),
    )


def _workflow_script(params: dict) -> str:
    script = params.get("workflow_script")
    if not isinstance(script, str) or not script:
        raise ValueError("distill.launch_args requires 'workflow_script': the absolute path of the distill-harvest script")
    if not Path(script).is_absolute() or not Path(script).is_file():
        raise ValueError(f"distill.launch_args: workflow_script {script!r} must be an absolute path to an existing file")
    return script


def _launch(payload: dict, script: str, launcher_name: str, **build_kw: Any) -> dict:
    run_dir = build_kw.get("run_dir") or Path(payload["repoRoot"]) / RUN_DIR_REL / payload["runId"]
    launcher = write_launcher(
        chunk_arrays({**payload, INLINED_KEY: True}), workflow_script=script, run_dir=Path(run_dir), file_name=launcher_name
    )
    built = build(payload, **build_kw)
    return {**launcher, "args_path": built["path"], "args_sha256": built["sha256"]}


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
