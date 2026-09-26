"""
coordinator_core.execute_plan_assemble — mutating assemblers for the
`/execute-plan` skill's write surfaces.

First member: `close_out_and_stamp`, the op that collapses `/execute-plan`
Phase 4's ordinal-narrated close-out sequence (stage every changed path,
stamp the plan `status:` to `implemented` when every chunk shipped, land
one scoped commit) into a single named op the skill invokes instead of
hand-sequencing git.

C6 (docs/plans/2026-09-11-document-scaffolding-is-emitted-not-remembered.md)
adds this module's own `brief()` — this host's scaffold-emission compute
for the one row `coordinator_core/ops/doctype_hosts.py` marks `emitted`
against `module="coordinator_core.execute_plan_assemble"`: `run-report`
(keyed `ceremony="execute-plan-assemble"`). `--plan`/`--chunk` are the two
flags the real parser enforces required for this type (`main()`'s own
`--plan <path> --chunk <id>` validation block); `--out` is likewise
required on this type (no CLI default — DEC-3's subsume removed the
retired guess) and is computed here from the same `(plan_stem, chunk_id)`
provision-key shape `subagent_sandbox.provision_report` derives at spawn
time, under the DR-091 one home every typed sidecar reader resolves
(`coordinator_core.session.machinery_paths.SHARE_RELDIR`) — never a
caller-supplied free-text path. Additive and gated on `plan_path`/
`chunk_id` both resolved, same shape as `roadmap_planning_assemble.
brief`'s C3 precedent.

Spec backlink: DoE-claude coordinator/skills/execute-plan/SKILL.md § Phase 4
Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C6.

Negative spec (C6's slice): does NOT decide whether `run-report` is
emitted at all — that is `coordinator_core.ops.doctype_hosts`'s (C0) table,
read by C8's coverage pin, never re-derived here. Does NOT call
`coordinator-doc-new` or any other CLI — a compute-half constructor only
(§ Which discriminator this plan uses). Does NOT touch
`subagent_sandbox.provision_report`'s own direct-write sidecar-provisioning
path — this is a separate, CLI-shaped scaffold-emission surface for
`/execute-plan`'s own dispatch step, not a second caller of that module.
Does NOT touch `close_out_and_stamp.py`/`row_spans.py` — this module's own
writes are scoped to `__init__.py` alone.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from coordinator_core.roadmap_planning_assemble.scaffold_directive import (
    Flag,
    build_scaffold_directive,
)
from coordinator_core.session.machinery_paths import SHARE_RELDIR

_RUN_REPORT_FLAG_SPEC: tuple[Flag, ...] = (
    Flag("--plan", "plan_path", required=True),
    Flag("--chunk", "chunk_id", required=True),
    Flag("--agent-type", "agent_type", required=False),
)


def _run_report_directive(
    plan_path: str,
    chunk_id: str,
    *,
    agent_type: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Computes the `run-report` scaffold directive through the shared
    constructor (C1), from `/execute-plan`'s own already-resolved
    `plan_path`/`chunk_id` dispatch-ledger pair — never a caller-supplied
    free-text argument (AC3).

    `--out` is computed here (never left unset -- this type has NO CLI
    default) under `SHARE_RELDIR` -- the DR-091 one home every typed
    subagent sidecar reader resolves -- keyed by `session_id` (an explicit
    override, else the first of `CLAUDE_SESSION_ID`/`CLAUDE_CODE_SESSION_ID`
    that resolves in the process environment, else the literal
    `"unknown-session"`; this is resolved PROCESS state, never a value the
    caller types), and the `<plan-stem>.<chunk-id>` provision-key shape
    `subagent_sandbox.provision_report` derives at spawn time -- mirrored
    here, not imported, since this compute-half never calls into that
    module's own direct-write path (module docstring's negative-spec).
    `--out` containment (AC4) and the `already_satisfied` existence
    predicate are the shared constructor's own job, not re-implemented
    here.
    """
    root = Path.cwd()
    resolved_session_id = (
        session_id
        or os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or "unknown-session"
    )
    plan_stem = Path(plan_path).stem
    out_path = os.path.join(
        *SHARE_RELDIR.split("/"),
        resolved_session_id,
        f"{plan_stem}.{chunk_id}.md",
    )
    resolved: dict[str, Any] = {
        "plan_path": plan_path,
        "chunk_id": chunk_id,
        "out": out_path,
    }
    if agent_type:
        resolved["agent_type"] = agent_type
    return build_scaffold_directive(
        "d-scaffold-run-report",
        "run-report",
        resolved,
        _RUN_REPORT_FLAG_SPEC,
        root=root,
    )


def brief(
    *,
    plan_path: Optional[str] = None,
    chunk_id: Optional[str] = None,
    agent_type: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    directives: list[dict[str, Any]] = []
    if plan_path and chunk_id:
        directives.append(
            _run_report_directive(
                plan_path, chunk_id, agent_type=agent_type, session_id=session_id
            )
        )
    return {"directives": directives}
