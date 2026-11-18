"""coordinator_core.learn_lessons_pipeline.ops — registers
`learn_lessons_pipeline.brief` (read-only), `learn_lessons_pipeline.apply`, and
`learn_lessons_pipeline.mechanical_close` plus its alias
`learn_lessons_pipeline.apply_mechanical` (all MUTATING) as warm-servable ops.

Purpose: this module's own import is the registration side-effect,
mirroring `coordinator_core.baton_assemble.ops` and
`coordinator_core.merge_assemble.ops` — `_registry_map.py`'s value for both
`learn_lessons_pipeline.brief` and `learn_lessons_pipeline.apply` points AT
this module, not at `__init__`/`apply` directly (C5).

Each handler is a thin `(params, repo_root) -> dict` adapter over the
existing `brief()`/`apply()` functions in `coordinator_core.
learn_lessons_pipeline` and `coordinator_core.learn_lessons_pipeline.apply`
— no new decision logic lives here. `repo_root` arrives already resolved
(per `coordinator_core.ipc`'s `_origin_worktree` envelope field, "show_top"
scope — see `op_scopes.py`) and is forwarded straight through as
`brief()`/`apply()`'s own `repo_root` positional argument.

Negative-spec:
    - Do NOT re-derive `brief()`/`apply()`'s decision logic here — this
      module is a registration/adapter seam only.
    - Do NOT resolve `repo_root` from `Path.cwd()`/`Path(__file__)` in
      either handler — the per-request resolved worktree root that arrives
      as this handler's own `repo_root` parameter is the only source, per
      the op-dispatch contract (`docs/wiki/coordinator-core-engine.md`:
      `repo_root` is an argument, not ambient context).
    - Do NOT add a `coordinator/bin/learn-lessons-*.py` front door here or
      anywhere else — no bin door, no `.cmd` twin, no `entry_point_shim.py`
      row (§ C5 body / § D1). If a front door is ever wanted, it is a
      separate row in a later plan.

Spec backlink: docs/plans/2026-09-11-the-lessons-pipeline-drains-without-a-ha.md § C5
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.group_em.repo_root_arg import RepoRootArgError, resolve_repo_root_arg
from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.learn_lessons_pipeline import brief as _brief
from coordinator_core.learn_lessons_pipeline.apply import apply as _apply
from coordinator_core.learn_lessons_pipeline.apply import mechanical_close, mechanical_close_dry_run


@register_op("learn_lessons_pipeline.brief")
async def _learn_lessons_pipeline_brief(
    params: dict[str, Any], repo_root: Optional[Path]
) -> dict[str, Any]:
    envelope = _brief(repo_root, roots=params.get("roots"))
    return {"exit_code": 0, "decision_object": envelope}


@register_op("learn_lessons_pipeline.apply")
async def _learn_lessons_pipeline_apply(
    params: dict[str, Any], repo_root: Optional[Path]
) -> dict[str, Any]:
    """MUTATING adapter over `learn_lessons_pipeline.apply.apply()` —
    recomputes the brief in-process and dispatches its `directives[]`
    through the closed `_DISPATCH_TABLE`, halting on the first non-zero
    exit (AC1).

    params:
        roots: optional list[str], forwarded to `apply(roots=...)`.
    """
    exit_code, report = _apply(repo_root, roots=params.get("roots"))
    return {"exit_code": exit_code, "report": report}


_MECHANICAL_CLOSE_PARAMS = {
    "type": "object",
    "properties": {"repo": {"type": "string"}, "dry_run": {"type": "boolean"}},
}


@register_op("learn_lessons_pipeline.mechanical_close", params_schema=_MECHANICAL_CLOSE_PARAMS)
@register_op("learn_lessons_pipeline.apply_mechanical", params_schema=_MECHANICAL_CLOSE_PARAMS)
async def _learn_lessons_pipeline_mechanical_close(
    params: dict[str, Any], repo_root: Optional[Path]
) -> dict[str, Any]:
    """MUTATING adapter over `mechanical_close` -- closes duplicate, content-free, wiki-append
    and out-of-enum-scope lesson rows in process, no model call; the report's `residue_ids`
    are the rows left for the agent and `unreadable` the rows whose YAML does not parse
    (named, never closed or repaired).

    params:
        repo: optional absolute path of the queue's repo root; defaults to the op's repo_root.
            A path that is not an existing directory is refused naming `repo`.
        dry_run: optional bool (default false); true returns verdicts and token counts
            without writing.
    """
    root = repo_root
    if params.get("repo") is not None:
        try:
            root = Path(resolve_repo_root_arg(params["repo"]))
        except RepoRootArgError as exc:
            raise CallerFacingValidationError(f"param `repo`: {exc}") from exc
    if params.get("dry_run"):
        return {"exit_code": 0, "report": mechanical_close_dry_run(root)}
    return {"exit_code": 0, "report": mechanical_close(root)}
