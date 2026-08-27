"""
coordinator_core.ops.strategic_generate — JSON-RPC "strategic.generate" operation.

Purpose: the claude-klabauter engine op that generates the machine-derivable subset of a repo's strategic
self-description draft — Track A (version_highlights[]) and Track B (competitors[] deltas) —
and writes it to `<target_root>/state/strategic/self-description.draft.yaml`. This is a
generator only: it never authors or touches the human-ratified canonical
`self-description.yaml`; reconciling draft into canonical is a separate, human-invoked ceremony
(coordinator:strategic-self-description-refresh skill), never this op.

Self-registration: importing this module calls register_op("strategic.generate", ...) as a
side-effect (same pattern as ops/ping.py). coordinator_core.ops.__init__ imports it so the
registration fires at start_server() time.

Classification: MUTATING (coordinator_core/authz/classification.py) — the handler writes
state/strategic/self-description.draft.yaml under the caller-supplied target_root via
draft_writer.write_draft. Mirrors percolate.run (MUTATING + scope "none").

Registration model (fleet-generic, PM-ratified Q2, coordinator-core-op-target-resolution-model):
scope "none" + explicit REQUIRED `target_root` wire param — mirrors the cartography.* /
workflow.* ops, NOT a common_dir-resolved op. The handler ignores repo_root/_origin_worktree
entirely and resolves solely from the caller-supplied, path_guard-contained target_root.

Wire params:
    target_root    (str, REQUIRED)  — root of the repo to generate the draft for. Path-guarded
                                       via coordinator_core.cartography._guard.path_guard.
    snapshot       (dict, optional) — current competitor snapshot, forwarded to
                                       derive_competitor_deltas (Track B input).
    prev_snapshot  (dict, optional) — prior competitor snapshot, forwarded to
                                       derive_competitor_deltas (Track B input).

Reply fields (result object in JSON-RPC response):
    target_root                (str) — resolved, guarded target_root, echoed.
    draft_path                 (str) — path to the written draft file.
    version_highlights_count   (int) — len(version_highlights derived).
    competitor_deltas_count    (int) — len(competitors derived).
    track_a_status              (str) — "no-highlights" | "populated".
    track_b_status               (str) — "no-delta" | "populated".

This chunk (C1) wires the op skeleton end-to-end with STUB helpers (version_highlights and
competitor_deltas both return [] unconditionally) so a wire-level dispatch smoke passes now,
producing a valid (empty-tracks) draft file. C2/C3 land the real Track A/B derivation logic; C4
hardens draft_writer. See each helper module's docstring for its own stub/hardening split.

Spec backlink: pln-claude-klabauter-generation-leg-machine--127c81 § C1
"""

from __future__ import annotations

from coordinator_core.cartography._guard import path_guard
from coordinator_core.ipc import register_op
from coordinator_core.ops.strategic.competitor_deltas import derive_competitor_deltas
from coordinator_core.ops.strategic.draft_writer import write_draft
from coordinator_core.ops.strategic.version_highlights import derive_version_highlights


@register_op("strategic.generate")
def _strategic_generate(params: dict, repo_root=None) -> dict:
    """JSON-RPC "strategic.generate" handler.

    Required params: target_root. See module docstring "Wire params" / "Reply fields".

    Raises ValueError if target_root is missing; propagates
    coordinator_core.cartography._guard.PathEscapeError if target_root cannot be resolved.
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("strategic.generate requires param: target_root")

    guarded = path_guard(target_root, ".")
    vh = derive_version_highlights(guarded)
    deltas = derive_competitor_deltas(guarded, params.get("snapshot"), params.get("prev_snapshot"))
    fields = {"version_highlights": vh, "competitors": deltas}
    draft_path = write_draft(guarded, fields)

    return {
        "target_root": str(guarded),
        "draft_path": str(draft_path),
        "version_highlights_count": len(vh),
        "competitor_deltas_count": len(deltas),
        "track_a_status": "no-highlights" if not vh else "populated",
        "track_b_status": "no-delta" if not deltas else "populated",
    }
