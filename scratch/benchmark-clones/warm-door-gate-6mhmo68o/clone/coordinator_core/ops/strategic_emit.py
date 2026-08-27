"""
coordinator_core.ops.strategic_emit — JSON-RPC "strategic.emit" operation.

Purpose: the claude-klabauter engine op that emits the curated strategic self-description canonical
(`<target_root>/state/strategic/self-description.yaml`) into a standalone
`strategic-emission.json` feed — the SIBLING of `cockpit-emission.json`
(coordinator_core/ops/emit/resolvers.py:569), NOT a section inside it (PM-ratified
2026-07-12 route decision: the closed cockpit-contract envelope schema forecloses adding a
strategic array without a DoE contract change + re-vendor; this op emits a separate feed that
reuses rag's ProvenanceEnvelope record shape instead). This is the emit sibling of
`strategic.generate` (coordinator_core/ops/strategic_generate.py), which only ever writes the
DRAFT — reconciling draft into the curated canonical is a separate, human-invoked ceremony
(coordinator:strategic-self-description-refresh) that is out of scope for both ops.

Self-registration: importing this module calls register_op("strategic.emit", ...) as a
side-effect (same pattern as ops/ping.py). coordinator_core.ops.__init__ imports it so the
registration fires at start_server() time.

Classification: MUTATING (coordinator_core/authz/classification.py) — the handler writes
`strategic-emission.json` under the caller-supplied target_root's central_state_root when the
canonical is present. Mirrors strategic.generate (MUTATING + scope "none").

Registration model (fleet-generic, PM-ratified Q2, coordinator-core-op-target-resolution-model):
scope "none" + explicit REQUIRED `target_root` wire param — mirrors strategic.generate/
cartography.*/workflow.* ops, NOT a common_dir-resolved op. The handler ignores
repo_root/_origin_worktree entirely and resolves solely from the caller-supplied,
path_guard-contained target_root.

Wire params:
    target_root    (str, REQUIRED)  — root of the repo to emit the strategic feed for.
                                       Path-guarded via
                                       coordinator_core.cartography._guard.path_guard.

Reply fields (result object in JSON-RPC response):
    target_root  (str) — resolved, guarded target_root, echoed.
    status       (str) — "no-canonical" | "emitted" (see emit_writer.emit_strategic_feed).
    emitted      (int) — 0 | 1.
    out          (str) — path to the written strategic-emission.json, present only when
                          status == "emitted".

If-exists posture (pinned, NOT this op's judgment call): canonical present -> emit; canonical
absent -> typed no-op, no file written, no crash. Absence is caught upstream by DoE's
repo-setup Phase 3l scaffold + workweek-complete Step 4i nag — this op never
synthesizes/curates/repairs the canonical itself.

Negative-spec: does NOT read `.draft.yaml` (see emit_writer.py's negative-spec). Does NOT put
curation-status in the ProvenanceEnvelope — that rides as record DATA inside the wrapped
canonical payload (emit_writer.build_feed).

Spec backlink: tasks/strategic-feed-emission/stub.md
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.cartography._guard import path_guard
from coordinator_core.ipc import register_op
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.strategic.emit_writer import emit_strategic_feed


@register_op("strategic.emit")
def _strategic_emit(params: dict, repo_root=None) -> dict:
    """JSON-RPC "strategic.emit" handler.

    Required params: target_root. See module docstring "Wire params" / "Reply fields".

    Raises ValueError if target_root is missing; propagates
    coordinator_core.cartography._guard.PathEscapeError if target_root cannot be resolved.
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("strategic.emit requires param: target_root")

    guarded = path_guard(target_root, ".")

    ctx = EmitContext.resolve(
        repo_root=guarded,
        coordinator_root=guarded,
        central_state_root=guarded / "state",
    )

    result = emit_strategic_feed(ctx)

    return {"target_root": str(guarded), **result}
