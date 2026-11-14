"""coordinator_core.ops.distill_bookkeeping -- `distill.bookkeeping`: the deterministic bookkeeping of a distill run.

MUTATING adapter that composes three shipped ops in-process, in order: `distill.apply_disposal` (the
stamped disposal manifest), `fleet.archive_completed_plans` (terminal plans to `archive/specs/`) and
`fleet.scratch_hygiene` (scratch residue). No LLM call, no agent dispatch, no subprocess hop between
steps. Judgment stays out: the disposal authority is the PM stamp already on the manifest, and plan
terminality is each plan's own `status:`.

params:
    run_id: the distill run's identifier (required).
    harvest_committed_sha: drain-ordering sha for the disposal step; default the newest commit touching
        the distillation log or the wiki tree (the disposal gate still verifies it).
    plan_cap: terminal plans archived per call (default 25).
    scratch_budget_secs: forwarded to `fleet.scratch_hygiene` as `budget_secs` (its own default when absent).

Each step reports `{"ran": bool, ...}`; a step that has nothing to do reports `ran: false` with a
`reason`, and a step that refuses or raises reports `error` and never stops the next step. `exit_code`
is 1 when any step errored, else 0; every step is idempotent, so a rerun converges.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op

DEFAULT_PLAN_CAP = 25
_DISPOSAL_MANIFEST_MISSING = "no disposal manifest for this run"
_NO_HARVEST_COMMIT = "no commit touches the distillation log or the wiki tree"


def _positive_int(params: dict, key: str, default: int) -> int:
    raw = params.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(f"{key} must be a positive int")
    return raw


async def _derive_harvest_sha(worktree: Path) -> Optional[str]:
    from coordinator_core.ops.distill_apply_disposal import (  # noqa: PLC0415
        CANONICAL_LOG_RELPATH,
        WIKI_TREE_PREFIX,
        _run_git,
    )
    from coordinator_core.ops.fleet._common import _make_git_env  # noqa: PLC0415

    rc, out, _err = await _run_git(
        "log", "-1", "--format=%H", "--", CANONICAL_LOG_RELPATH, WIKI_TREE_PREFIX.rstrip("/"),
        cwd=worktree, env=_make_git_env(),
    )
    sha = out.decode("utf-8", errors="replace").strip()
    return sha if rc == 0 and sha else None


async def _disposal_step(common_dir: Path, worktree: Path, run_id: str, harvest: Optional[str]) -> dict[str, Any]:
    from coordinator_core.ops.distill_apply_disposal import _handler as apply_disposal  # noqa: PLC0415
    from coordinator_core.ops.distill_stamp_disposal import manifest_path_for_run  # noqa: PLC0415

    if not manifest_path_for_run(worktree, run_id).is_file():
        return {"ran": False, "reason": _DISPOSAL_MANIFEST_MISSING}
    sha = harvest or await _derive_harvest_sha(worktree)
    if not sha:
        return {"ran": False, "error": _NO_HARVEST_COMMIT}
    try:
        result = await apply_disposal({"run_id": run_id, "harvest_committed_sha": sha}, repo_root=common_dir)
    except ValueError as exc:
        return {"ran": False, "error": str(exc)}
    return {"ran": True, "harvest_committed_sha": sha, **result}


def _spec_archive_step(common_dir: Path, plan_cap: int) -> dict[str, Any]:
    from coordinator_core.ops.fleet.archive_plans import _handler as archive_plans  # noqa: PLC0415

    base = {"mode": "already-terminal", "cap": plan_cap}
    preview = archive_plans({**base, "dry_run": True}, repo_root=common_dir)
    if preview.get("exit_code") != 0:
        return {"ran": False, "error": "archive_completed_plans preview refused", "result": preview}
    if preview.get("contended"):
        return {"ran": False, "reason": "another archive sweep holds the lock"}
    ids = [c["id"] for c in preview.get("candidates", [])]
    if not ids:
        return {"ran": False, "reason": "no terminal plans", "skipped": preview.get("skipped", [])}
    result = archive_plans({**base, "dry_run": False, "candidate_ids": ids}, repo_root=common_dir)
    step: dict[str, Any] = {
        "ran": True,
        "acted": result.get("acted", []),
        "skipped": result.get("skipped", []),
        "failed": result.get("failed", []),
    }
    if result.get("exit_code") != 0:
        step["error"] = f"archive_completed_plans exit_code {result.get('exit_code')}"
    return step


def _scratch_step(worktree: Path, budget_secs: Any) -> dict[str, Any]:
    from coordinator_core.ops.fleet.scratch_hygiene import run_hygiene  # noqa: PLC0415

    params: dict[str, Any] = {"repo_root": str(worktree), "cadence": "distill", "apply": True}
    if budget_secs is not None:
        params["budget_secs"] = budget_secs
    result = run_hygiene(params)
    if result.get("exit_code") != 0:
        return {"ran": False, "error": result.get("error", "scratch_hygiene refused")}
    return {"ran": True, "walk": result.get("walk", {}), "summary": result["records"][-1]}


@register_op("distill.bookkeeping")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "distill.bookkeeping" handler; `repo_root` is the git common dir (`_OP_KEY_SCOPE="common_dir"`)."""
    if repo_root is None:
        raise ValueError("distill.bookkeeping requires a resolved repo_root")
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    run_id = run_id.strip()
    harvest = params.get("harvest_committed_sha")
    if harvest is not None and (not isinstance(harvest, str) or not harvest.strip()):
        raise ValueError("harvest_committed_sha must be a non-empty string")
    plan_cap = _positive_int(params, "plan_cap", DEFAULT_PLAN_CAP)

    from coordinator_core.ops.fleet._common import main_worktree_root  # noqa: PLC0415

    common_dir = Path(repo_root)
    worktree = main_worktree_root(common_dir)

    steps: dict[str, dict[str, Any]] = {}
    steps["disposal"] = await _disposal_step(common_dir, worktree, run_id, harvest)
    # archive_plans runs asyncio.run on its act path, so it needs a thread without a running loop.
    steps["spec_archive"] = await asyncio.to_thread(_spec_archive_step, common_dir, plan_cap)
    steps["scratch"] = await asyncio.to_thread(_scratch_step, worktree, params.get("scratch_budget_secs"))
    errored = [name for name, step in steps.items() if "error" in step]
    return {"run_id": run_id, "exit_code": 1 if errored else 0, "errored": errored, "steps": steps}
