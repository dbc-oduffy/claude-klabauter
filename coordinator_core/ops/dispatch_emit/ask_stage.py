"""dispatch.ask_stage: a plan or an XS sizing -> brief files, manifest.json and commit request.

Everything lands under `RUN_DIR_ROOT/<run_id>/`: `briefs/<row>.md` (the plan route's row prompt),
`commit-request.txt` (the `render_marker` line, empty when no row declares a path) and
`manifest.json` (a `StageManifest`). In-process file writes only; no git, no subprocess, so the
op holds the 500ms bar. Trap: unlike `compose_script`, the per-row pathspec is not
gitignore-filtered, because that filter spawns git.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.git.git_state import head_branch
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RUN_DIR_ROOT,
    ManifestRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
    CrossRepoWriteError,
    check_cross_repo_writes,
    check_external_gate_exclusions,
)
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.dispatch_emit.emit import (
    _AGENT_MODELS,
    _dispatch_report_path,
    _plan_deliverable_id,
    _dedupe_preserve_order,
    _model_opt,
    _plan_id,
    _row_agent_type,
    _row_prompt,
    _widen_with_test_candidates,
    check_unschedulable_rows,
    derive_plan_context,
)
from coordinator_core.ops.dispatch_emit.pathspec import commit_pathspec_or_none
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.dispatch_emit.self_dr_discharge import discharge_clauses
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, load_sizing
from coordinator_core.ops.dispatch_emit.sizing_xs_mint import mint_xs_spine
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.dispatch_emit.wave_map import build_waves
from coordinator_core.ops.plan_tasks_render import load_rows

_PARAMS = (
    Field("run_id", "str", required=True),
    Field("plan_path", "str"),
    Field("sizing_path", "str"),
    Field("writes", "str_list"),
)

__all__ = ["AskStageError", "stage"]


class AskStageError(ValueError):
    """The stage inputs name no stageable plan or sizing, or escape the run dir."""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _discharge_clauses_for(plan_text: str, root: Path, raw_rows: list) -> dict[str, str]:
    """Self-DR discharge clauses keyed by row id; {} without a readable `sizing_object`."""
    split = split_frontmatter(plan_text)
    cited = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    if not cited:
        return {}
    try:
        sizing = load_sizing(root, cited)
    except SizingFireRefused:
        return {}
    return discharge_clauses(raw_rows, sizing)


def stage(
    repo_root: Path,
    *,
    run_id: str,
    plan_rel: Optional[str] = None,
    sizing_rel: Optional[str] = None,
    writes: Sequence[str] = (),
) -> StageManifest:
    """Stage the run for `plan_rel`, or for the XS sizing `sizing_rel` (minted to X1 over `writes`)."""
    root = Path(repo_root)
    if not safe_id(run_id):
        raise AskStageError(f"run_id {run_id!r} is not a single safe path segment")
    if bool(plan_rel) == bool(sizing_rel):
        raise AskStageError("name exactly one of plan_rel and sizing_rel")

    run_dir = contained_path(root / RUN_DIR_ROOT / run_id, [root / RUN_DIR_ROOT])
    if run_dir is None:
        raise AskStageError(f"run dir for {run_id!r} escapes {RUN_DIR_ROOT}")

    if sizing_rel:
        sizing = load_sizing(root, sizing_rel)
        spine_text, spine_path = mint_xs_spine(
            sizing, sizing_rel=sizing_rel, writes=writes, out_dir=run_dir
        )
        _write(spine_path, spine_text)
        plan_path = spine_path
    else:
        plan_path = contained_path(root / plan_rel, [root])
        if plan_path is None or not plan_path.is_file():
            raise AskStageError(f"plan not found under the repo: {plan_rel!r}")
    plan_posix = _rel(root, plan_path)

    plan_text = plan_path.read_text(encoding="utf-8")
    exclusions: list = []
    rows = read_spine(plan_path, exclusions=exclusions)
    raw_by_id = {
        raw["id"]: raw
        for raw in load_rows(plan_text).rows
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    }
    check_unschedulable_rows(rows, raw_by_id)
    try:
        check_external_gate_exclusions(exclusions, raw_by_id)
        check_cross_repo_writes(rows, root)
    except CrossRepoWriteError as exc:
        raise AskStageError(str(exc)) from exc
    waves = build_waves(rows)
    if not waves:
        raise AskStageError("the spine derives zero dispatchable rows")

    context = derive_plan_context(
        plan_text, fallback_title=plan_path.stem, repo_root=root.as_posix()
    )

    clauses = _discharge_clauses_for(plan_text, root, list(raw_by_id.values()))

    manifest_rows: list[ManifestRow] = []
    chunks: list[ChunkCommit] = []
    declared: list[str] = []
    for wave_no, wave in enumerate(waves):
        for row in wave:
            agent_type = _row_agent_type(row)
            _model_opt(agent_type, row.agent_model)  # raises on an unknown agent type or bad model grammar
            model = row.agent_model or _AGENT_MODELS[agent_type]
            brief = run_dir / "briefs" / f"{row.id}.md"
            text = _row_prompt(row, plan_posix, context)
            if row.id in clauses:
                text = text.rstrip() + "\n\n" + clauses[row.id] + "\n"
            _write(brief, text)
            paths = _widen_with_test_candidates(commit_pathspec_or_none([row]) or [])
            declared.extend(paths)
            manifest_rows.append(
                ManifestRow(
                    id=row.id,
                    agent_type=agent_type,
                    model=model,
                    brief_path=_rel(root, brief),
                    writes=tuple(paths),
                    wave=wave_no,
                )
            )
            chunks.append(
                ChunkCommit(
                    id=row.id,
                    title=row.title,
                    paths=tuple(paths),
                    prefixes=tuple(row.writes_under),
                    report=_dispatch_report_path(plan_posix, row.id),
                )
            )

    branch = head_branch(root)
    marker = render_marker(
        CommitRequest(
            chunks=tuple(chunks),
            deliverable_id=_plan_deliverable_id(plan_text),
            repo_root=root.as_posix(),
            plan_path=plan_posix,
            expected_branch=None if branch in (None, "HEAD") else branch,
        )
    )
    marker_path = run_dir / "commit-request.txt"
    _write(marker_path, f"{marker}\n" if marker else "")

    manifest = StageManifest(
        run_dir=_rel(root, run_dir),
        rows=tuple(manifest_rows),
        review_declared_paths=tuple(_dedupe_preserve_order(declared)),
        marker_path=_rel(root, marker_path),
        plan_id=_plan_id(plan_text),
    )
    _write(run_dir / "manifest.json", json.dumps(manifest.to_json(), indent=2) + "\n")
    return manifest


@register_op("dispatch.ask_stage")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.ask_stage": params `run_id` (str), exactly one of `plan_path` / `sizing_path`, `writes`.

    Replies `StageManifest.to_json()`; a refused request replies `{"error": str}`.
    """
    if repo_root is None:
        return {"error": "dispatch.ask_stage requires repo_root"}
    refusal = validate_params("dispatch.ask_stage", params, _PARAMS)
    if refusal is not None:
        return refusal
    writes = params.get("writes") or []
    run_id = params["run_id"]
    try:
        manifest = stage(
            main_worktree_root(Path(repo_root)),
            run_id=run_id,
            plan_rel=params.get("plan_path") or None,
            sizing_rel=params.get("sizing_path") or None,
            writes=writes,
        )
    except (AskStageError, SizingFireRefused) as exc:
        return {"error": str(exc)}
    return manifest.to_json()
