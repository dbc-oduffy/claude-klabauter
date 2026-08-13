"""
coordinator_core.ops.emit_cadence — JSON-RPC "emit.cadence" operation.

Purpose: a thin composite sequencer over the two existing, separately-registered emit ops
(backlog.record, artifact.emit). Guarantees the emission-semantic invariant that a single
cadence invocation records today's backlog-depth snapshot BEFORE the cockpit-emission
aggregation runs — so the aggregate always reflects the same-invocation's freshly-written
row (no missed same-day snapshot). This ordering was previously unguaranteed: the two ops
were dark/unsequenced, and nothing on either plane fired them together.

This op deliberately does NOT fold backlog.record and artifact.emit into one handler — both
stay exactly as they are, separately registered (ops/__init__.py) and separately classified
(authz/classification.py). emit.cadence is a sequencer that calls the unmodified public
entry points of each (recorder.record, envelope.emit) over one shared EmitContext, resolved
once and reused, so context resolution is not duplicated across the two sub-ops.

Decision provenance: PM-ratified per-repo tri-plane boundary (2026-07-03) — emission
ordering semantics live on claude-klabauter's control plane, not a coordinator-claude ceremony facade. See the
actioned memo response (decision B) at
cross-repo/inbox/2026-07-11-claude-central-em-backlog-record-cadence-relocation-reconcile.md
§ EM Response.

Self-registration: importing this module calls register_op("emit.cadence", ...) as a
side-effect (same pattern as ops/artifact_emit.py / ops/emit/recorder.py). coordinator_core.
ops.__init__ imports it so the registration fires at start_server() time.
"""

from __future__ import annotations

from coordinator_core.ipc import register_op
from coordinator_core.ops.emit import envelope
from coordinator_core.ops.emit.envelope import resolve_context
from coordinator_core.ops.emit.recorder import record
from coordinator_core.ops.fleet._common import main_worktree_root


@register_op("emit.cadence")
def _emit_cadence(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'emit.cadence' handler — record today's backlog snapshot, then aggregate.

    Params (all optional):
        out (str) — override output path for the aggregation step; forwarded to
        envelope.emit's --out semantics.

    repo_root (injected by ipc.dispatch_message): git_common_dir of the originating worktree.
    The handler derives the main-worktree root via main_worktree_root(repo_root) — same
    derivation as artifact.emit / backlog.record — and resolves ONE EmitContext, reused for
    both sub-ops (avoids double context resolution). Fails loud when repo_root is None
    (AC5 — no silent fallback to meta-repo).

    Sequencing is load-bearing: record(ctx) runs first, then envelope.emit(ctx, out=...),
    so the aggregate always includes the row this same invocation just wrote. Returns both
    sub-op results under distinct keys — this op does not merge or reshape either result.

    Cadence tier: the two cadence-gated enrichment stages
    (``envelope._stamp_docs_staleness`` and the ``file_attribution`` section) compute for real
    here. The explicit ``ctx.full_enrichment = True`` below is REDUNDANT, not load-bearing —
    ``EmitContext.full_enrichment`` defaults to True since the 2026-07-29 safe-default reversal,
    so removing the line would not change this op's behaviour today. It is kept because this op
    is the ceremony-boundary emission and its tier is a property of the ceremony, not something
    to inherit silently from a dataclass a future edit could flip. Do not cite this line as
    evidence that the cheap tier is the ambient default; it is not.
    """
    if repo_root is None:
        raise ValueError(
            "emit.cadence requires a per-repo dispatch key (_origin_worktree); "
            "repo_root is None. No silent fallback to meta-repo (AC5)."
        )
    derived_root = main_worktree_root(repo_root)
    ctx = resolve_context(derived_root)
    ctx.full_enrichment = True

    record_result = record(ctx)
    emit_result = envelope.emit(ctx, out=params.get("out"))

    return {"backlog_record": record_result, "artifact_emit": emit_result}
