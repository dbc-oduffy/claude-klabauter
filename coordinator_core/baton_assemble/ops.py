"""coordinator_core.baton_assemble.ops — registers `baton_assemble.brief`
(read-only) and `baton_assemble.apply` (MUTATING) as warm-servable ops.

Before this module, `baton_assemble` registered NO op at all — the only way
to reach `brief()`/`apply()` was `coordinator/bin/lib/entry_point_shim.py`'s
`_simple_entry("baton-assemble", "coordinator_core.baton_assemble")`, a thin
shim over `entry_point_shim.py::run_target` that runs IN-PROCESS in a COLD
interpreter per invocation (never through the warm engine's UDS transport).
Registering these two ops makes `baton_assemble` REACHABLE through the warm
engine. The forwarder keeps working unchanged (two-consumer rule); this
module adds a second consumer, it does not replace the first.

Purpose: this module's own import is the registration side-effect, mirroring
`coordinator_core.merge_assemble.ops` (chunk C6,
docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md).

Each handler is a thin `(params, repo_root) -> dict` adapter over the
existing `brief()`/`apply()` functions in `coordinator_core.baton_assemble`
and `coordinator_core.baton_assemble.apply` — no new decision logic lives
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

Spec backlink: make baton_assemble reachable through the warm engine
(reachability chunk; parallels merge_assemble's chunk C6).

Reachability note (2026-08-30): registering these two keys did NOT fix a cold
door. `baton-assemble` was already on `ops/warm_entrypoint_allowlist.json` and
already warm-served via `invoke_from_argv.py`'s native-invocation surface, which
runs `baton_assemble.main(argv)` in-process. What this module adds is a SECOND
reachability path -- structured JSON-RPC op dispatch through `get_op_handler`,
carrying authz classification and repo-root scoping -- alongside the argv one.
Do not cite this module as a latency improvement; the 156.2ms import figure that
motivated it was the cold forwarder's cost, a door the `.exe` does not use.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.baton_assemble import BriefResult, TransportFailure
from coordinator_core.baton_assemble import brief as _brief
from coordinator_core.baton_assemble.apply import apply as _apply


@register_op("baton_assemble.brief")
async def _baton_assemble_brief(params: dict[str, Any], repo_root: Optional[Path]) -> dict[str, Any]:
    """Read-only adapter over `baton_assemble.brief()`. Mutates nothing.

    params:
        kind:                    required str, one of {"handoff", "spinoff"}.
        artifact_path:           optional str (falsy self-resolves for
                                  kind="handoff" — see `brief()`'s own
                                  docstring), default "".
        decisions:               optional dict, forwarded to `brief(decisions=...)`.
        title:                   optional str, forwarded to `brief(title=...)`.
        explicit_deliverable_id: optional str, forwarded to
                                  `brief(explicit_deliverable_id=...)`.
        session_id:              optional str, forwarded to `brief(session_id=...)`.
    """
    try:
        result: BriefResult = _brief(
            kind=params["kind"],
            artifact_path=params.get("artifact_path") or "",
            decisions=params.get("decisions"),
            repo_root=repo_root,
            title=params.get("title"),
            explicit_deliverable_id=params.get("explicit_deliverable_id"),
            session_id=params.get("session_id"),
        )
    except TransportFailure as exc:
        return {"exit_code": 1, "error": str(exc)}
    return {"exit_code": result.exit_code, "decision_object": result.decision_object}


@register_op("baton_assemble.apply")
async def _baton_assemble_apply(params: dict[str, Any], repo_root: Optional[Path]) -> dict[str, Any]:
    """MUTATING adapter over `baton_assemble.apply.apply()` — recomputes the
    brief in-process and dispatches its `directives[]` through the closed
    `_CLI_DISPATCH` table (see that module's own docstring), mutating
    handoff/spinoff artifacts, the claim ledger, and archiving predecessors.

    params:
        kind:                    required str, one of {"handoff", "spinoff"}.
        artifact_path:           required str, forwarded to `apply(artifact_path=...)`.
        session_id:              optional str, forwarded to `apply(session_id=...)`.
        decisions:               optional dict, forwarded to `apply(decisions=...)`.
        title:                   optional str, forwarded to `apply(title=...)`.
        explicit_deliverable_id: optional str, forwarded to
                                  `apply(explicit_deliverable_id=...)`.
    """
    exit_code, report = _apply(
        kind=params["kind"],
        artifact_path=params.get("artifact_path", ""),
        session_id=params.get("session_id"),
        repo_root=repo_root,
        decisions=params.get("decisions"),
        title=params.get("title"),
        explicit_deliverable_id=params.get("explicit_deliverable_id"),
    )
    return {"exit_code": exit_code, "report": report}
