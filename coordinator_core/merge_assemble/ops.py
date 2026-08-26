"""coordinator_core.merge_assemble.ops — registers `merge_assemble.brief` and
`merge_assemble.apply` as warm-servable ops.

Purpose: this module's own import is the registration side-effect (chunk C6,
docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md).
Before this chunk, `merge_assemble` registered no op at all — the only way
to reach `brief()`/`apply()` was `coordinator/bin/merge-assemble.py`, a thin
shim over `coordinator/bin/lib/entry_point_shim.py::run_target` that runs
IN-PROCESS in a COLD interpreter per ceremony invocation (never through the
warm engine's UDS transport). Registering these two ops makes a ceremony
REACHABLE through the warm engine — REACHED is a separate question this
chunk explicitly defers to C7, which measures the path a real ceremony
actually takes. The forwarder keeps working unchanged (two-consumer rule);
this module adds a second consumer, it does not replace the first.

Each handler is a thin `(params, repo_root) -> dict` adapter over the
existing `brief()`/`apply()` functions in `coordinator_core.merge_assemble`
and `coordinator_core.merge_assemble.apply` — no new decision logic lives
here. `repo_root` arrives already resolved (per `coordinator_core.ipc`'s
`_origin_worktree` envelope field, "show_top" scope — see `op_scopes.py`)
and is passed straight through as `brief()`/`apply()`'s own `repo_root`
keyword; when it is `None` (an out-of-repo request, or a CLI caller that
never resolved one) each function's own `resolve_repo_root()` fallback
still applies, matching today's CLI-invoked behaviour exactly.

Negative-spec:
    - Do NOT re-derive `brief()`/`apply()`'s decision logic here — this
      module is a registration/adapter seam only.
    - Do NOT resolve `repo_root` from `Path.cwd()`/`Path(__file__)` in
      either handler — the per-request resolved worktree root that arrives
      as this handler's own `repo_root` parameter is the only source, per
      the op-dispatch contract (`docs/wiki/coordinator-core-engine.md`:
      `repo_root` is an argument, not ambient context).

Spec backlink: docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md, chunk C6
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.merge_assemble import brief as _brief
from coordinator_core.merge_assemble.apply import apply as _apply


@register_op("merge_assemble.brief")
async def _merge_assemble_brief(params: dict[str, Any], repo_root: Optional[Path]) -> dict[str, Any]:
    """COMPUTE_ONLY adapter over `merge_assemble.brief()` — recomputes the
    read-only merge decision object (branch_state, version_bump proposal,
    directives[], judgment_points[]) and returns it verbatim; writes nothing.

    params:
        decisions:  optional dict, forwarded to `brief(decisions=...)`.
        tag_prefix: optional str, default "v", forwarded to `brief(tag_prefix=...)`.
    """
    result = _brief(
        decisions=params.get("decisions"),
        repo_root=repo_root,
        tag_prefix=params.get("tag_prefix", "v"),
    )
    return {"exit_code": result.exit_code, "decision_object": result.decision_object}


@register_op("merge_assemble.apply")
async def _merge_assemble_apply(params: dict[str, Any], repo_root: Optional[Path]) -> dict[str, Any]:
    """MUTATING adapter over `merge_assemble.apply.apply()` — recomputes the
    brief in-process and dispatches its `directives[]` through the closed
    `_CLI_DISPATCH` table (see that module's own docstring), mutating branch
    state, cutting tags, and minting/handing back the ceremony's Tier-U grant.

    params:
        session_id: optional str, forwarded to `apply(session_id=...)`.
        decisions:  optional dict, forwarded to `apply(decisions=...)`.
        force:      optional bool, default False, forwarded to `apply(force=...)`.
        tag_prefix: optional str, default "v", forwarded to `apply(tag_prefix=...)`.
    """
    exit_code, report = _apply(
        session_id=params.get("session_id"),
        repo_root=repo_root,
        decisions=params.get("decisions"),
        force=bool(params.get("force", False)),
        tag_prefix=params.get("tag_prefix", "v"),
    )
    return {"exit_code": exit_code, "report": report}
