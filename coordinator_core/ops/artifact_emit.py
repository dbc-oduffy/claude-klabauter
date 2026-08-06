"""
coordinator_core.ops.artifact_emit — JSON-RPC "artifact.emit" operation.

Purpose: the claude-klabauter engine op that produces the authoritative cockpit-emission.json artifact.
This is a MUTATING op — it writes the canonical
work-state snapshot to disk (DR § DD#2 "emission write = claude-klabauter engine"; authz classification
MUTATING in coordinator_core/authz/classification.py). MUTATING ops write coordinator substrate
ONLY, never rag's relational store (dual-write ban, DR-208 / tri-plane DD#1).

Self-registration: importing this module calls register_op("artifact.emit", ...) as a
side-effect (same pattern as ops/ping.py). coordinator_core.ops.__init__ imports it so the
registration fires at start_server() time.

C4a WIRING: per-repo emission cutover (2026-07-07). The handler now derives
main_worktree_root(common_dir) from the dispatch-provided key (repo_root = git_common_dir
injected by ipc.py for common_dir-scoped ops) and passes it directly to
envelope.resolve_context(). Fail-loud when repo_root is None (AC5 — unresolvable key is a
corruption risk; no silent meta-repo fallback).

Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § C1 / C2
Amendment:     docs/plans/2026-07-07-per-repo-emission-cutover.md § C4a
"""

from __future__ import annotations

from coordinator_core.ipc import register_op
from coordinator_core.ops.emit import envelope as _envelope
from coordinator_core.ops.fleet._common import main_worktree_root


@register_op("artifact.emit")
async def _artifact_emit(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'artifact.emit' handler — produce the cockpit-emission.json artifact.

    Params (all optional):
        out (str) — override output path; bypasses the sentinel gate (bash --out semantics).
        full_enrichment (bool, DEFAULT True) — cadence tier for this emission. True computes
            every enrichment for real; False opts into the cheap tier, where the two most
            expensive enrichments (``envelope._stamp_docs_staleness`` and the
            ``file_attribution`` section) are satisfied from last-known values per
            ``ops/emit/skipped_stage.py``'s one invariant.

    The ``True`` default here is redundant with ``EmitContext.full_enrichment``'s own default
    and is kept anyway, deliberately: this op's tier is part of its published contract (an op
    documented as authoritative must not quietly become "authoritative as of the last cadence
    run"), so it states its tier at the call site rather than inheriting one that a future
    dataclass edit could change underneath it. The redundancy is the point — if the two ever
    disagree, this line wins and the disagreement is visible in one file.
    Review: code-reviewer (Finding 3) — before this param there was no way to ask for the full
    tier through this op at all; it had been silently demoted to the cheap tier by the
    cadence-gate work. That silent demotion is also why the dataclass default was later
    reversed to True (see ``EmitContext.full_enrichment``) — this op was the live instance of
    the trap that default set.

    repo_root (injected by ipc.dispatch_message): git_common_dir of the originating worktree.
    The handler derives the main-worktree root via main_worktree_root(repo_root) — a linked
    worktree's .git is a file; using the raw _origin_worktree directly would silently emit
    zero records (lesson 2026-07-05-common-dir-keyed-ops-must-derive-the-wor.yaml).
    Fails loud when repo_root is None (AC5 — no silent fallback to meta-repo).

    Delegates to coordinator_core.ops.emit.envelope: resolves the per-repo run context,
    asserts the vendored contract pin, assembles the envelope from the section registry,
    gates on the re-vendor sentinel when writing the canonical path, and writes the artifact.
    Returns the envelope's summary result.
    """
    if repo_root is None:
        raise ValueError(
            "artifact.emit requires a per-repo dispatch key (_origin_worktree); "
            "repo_root is None — op scope must be 'common_dir' and _origin_worktree must be "
            "present in the JSON-RPC envelope. No silent fallback to meta-repo (AC5)."
        )
    full_enrichment = params.get("full_enrichment", True)
    if not isinstance(full_enrichment, bool):
        raise ValueError(
            "artifact.emit: full_enrichment must be a boolean, got "
            f"{type(full_enrichment).__name__} ({full_enrichment!r}). Fail loud rather than "
            "coerce — a truthy string would silently select the opposite tier from the one "
            "the caller wrote."
        )

    derived_root = main_worktree_root(repo_root)
    emit_ctx = _envelope.resolve_context(derived_root)
    emit_ctx.full_enrichment = full_enrichment
    out = params.get("out")
    result = _envelope.emit(emit_ctx, out=out)

    # Self-report scope-touch contract (design (b), 2026-08-04 — see
    # coordinator_core.ipc's module-level comment above `_SCOPE_TOUCH_PATHS_KEY`).
    # `envelope.emit`'s sole write is `out_path.write_text(...)`, recorded on its
    # return dict's `out` key — declare exactly that one path, never a hardcoded
    # constant, so the declaration always matches whatever out_dir this call
    # actually resolved to (COCKPIT_SCHEMA_OUT_DIR / the `out` param / the
    # per-repo default). Recovered from the brief's misattribution to
    # `emit/sections/handoffs.py` — see docs/plans/2026-08-05-
    # in-process-writers-declare-their-writes.md § Substrate corrections; that
    # module remains in-process/unfixable, but `artifact_emit` itself transits
    # `dispatch_message` (registered as "artifact.emit") and so has this seam.
    # `dispatch_message` strips this key before the wire envelope is built.
    emitted_out = result.get("out")
    if isinstance(emitted_out, str) and emitted_out:
        result["_scope_touch_paths"] = [emitted_out]
    return result
