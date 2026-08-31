"""
coordinator_core.ops.tracker.completion_policy — tracker.assert_code_complete op.

Purpose: DR-318 §D2's routed obligation (`docs/decisions/
DR-318-sat-04-completion-axis-policy-reachabili.md:96-100`) — sat-04 shipped
`coordinator_core.tracker_completion_policy` as pure functions plus one
impure seam, `emit_code_complete_assert`, and deliberately registered no op:
"the op surface is sat-06's remit." This module is that op surface. It
registers ONLY `emit_code_complete_assert` — the tier classifier
(`classify_code_complete_tier`), the qa_verified stub (`classify_qa_verified`)
and the pure retract-payload builder (`detect_symmetric_retract`) stay
unregistered, exactly as `tracker_completion_policy.py`'s own module
docstring scopes this chunk's D4 note ("wiring the RETRACT path through the
emit seam is sat-06/sat-07's remit").

Classification ruling (C2, DR-241 amendment dated 2026-08-20; governs this op
equally per this plan's own C11 body): MUTATING. Unlike `tracker.render_status`
this is not even the conservative-by-construction case — this op's own
handler body appends a transition event via `tracker_completion_policy.
emit_code_complete_assert` -> `tracker_transitions.emit_transition` -> `_emit`,
a real write. This op is also the live claude-klabauter-internal consumer AC4 asked
after: registering it creates the first real caller chain into
`tracker_completion_policy` where none existed before.

This module reaches the policy layer ONLY through
`coordinator_core.tracker_completion_policy.emit_code_complete_assert` —
never `tracker_transitions` directly, and never the underlying
sovereign-tracker append/read module directly or by hand-built
`state/sovereign-tracker/` path literal.

Spec backlink: docs/plans/2026-08-18-sat-06-cockpit-consumption-seam.md
  § Tasks C11, § Acceptance Criteria AC4/AC5/AC11.
Spec backlink: docs/decisions/DR-318-sat-04-completion-axis-policy-reachabili.md
  § D2 — the routed obligation this op discharges.
Spec backlink: docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
  § Amendment (2026-08-20) — the MUTATING ruling this op's registration
  discharges (C2, applied here per C11's own body).

Negative-spec — a future editor must NOT:
  - Register `classify_code_complete_tier`, `classify_qa_verified`, or
    `detect_symmetric_retract` as ops here — this chunk registers the ASSERT
    seam only (D4); wiring the revert-driven retract through the emit seam
    is sat-06/sat-07's remit, not this chunk's.
  - Import `coordinator_core.tracker_transitions` directly from this module
    — go through `tracker_completion_policy.emit_code_complete_assert` only.
  - Import the underlying sovereign-tracker append/read module directly, or
    hand-build a `state/`/`archive/` path literal (its module name must not
    appear anywhere in this module, including docstrings — AC11).
  - Reclassify this op to `OpClass.COMPUTE_ONLY` — it performs a real write
    on every call; there is no read-only path through this handler.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.tracker_completion_policy import (
    CodeCompleteEvidence,
    emit_code_complete_assert,
)
from coordinator_core.tracker_entities import CLOSURE_FIDELITY_VALUES
from coordinator_core.tracker_projection import DEFAULT_CLOSURE_FIDELITY

# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("tracker.assert_code_complete")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """tracker.assert_code_complete — classify and emit a `code_complete`
    ASSERT observation through `tracker_completion_policy.
    emit_code_complete_assert` (see module docstring for the full contract).

    Wire contract:
        params: {
            "item_id": str,
            "sha": str,
            "trailer_bound": bool,
            "reachable_on_default_branch": bool | None,
            "actor": str,
            "source_observation_id": str | None (optional),
            "closure_fidelity": str (optional — one of
                `CLOSURE_FIDELITY_VALUES`; omitted means
                `DEFAULT_CLOSURE_FIDELITY`),
            "repo_root": str (optional — D3 consistency check only),
        }
        ->      the stored transition event dict `emit_code_complete_assert`
                returns. On a D3 mismatch:
                {"asserted": False, "reason": <mismatch string>}.

    `repo_root` handler arg is the git common dir (`_OP_KEY_SCOPE:
    "common_dir"`); the worktree root is derived via
    `main_worktree_root(repo_root)` — never from `params.repo_root`.

    Raises:
        RuntimeError — repo_root is None (engine misconfiguration; production
            always supplies it via `_OP_KEY_SCOPE='common_dir'`).
        ValueError — item_id/sha/actor missing or malformed.
        TrackerTransitionError — propagated from `tracker_transitions`.
    """
    if repo_root is None:
        raise RuntimeError(
            "tracker.assert_code_complete: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production; "
            "test fixtures must supply an explicit value"
        )
    common_dir = Path(repo_root)
    worktree = main_worktree_root(common_dir)

    # D3: optional repo_root consistency check (contract §3.3 doctrine).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return {"asserted": False, "reason": mismatch}

    item_id = params.get("item_id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError(
            f"tracker.assert_code_complete: item_id must be a non-empty string, got {item_id!r}"
        )

    sha = params.get("sha")
    if not isinstance(sha, str) or not sha.strip():
        raise ValueError(
            f"tracker.assert_code_complete: sha must be a non-empty string, got {sha!r}"
        )

    actor = params.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError(
            f"tracker.assert_code_complete: actor must be a non-empty string, got {actor!r}"
        )

    trailer_bound = bool(params.get("trailer_bound", False))
    reachable_on_default_branch = params.get("reachable_on_default_branch")
    if reachable_on_default_branch is not None and not isinstance(
        reachable_on_default_branch, bool
    ):
        raise ValueError(
            "tracker.assert_code_complete: reachable_on_default_branch must be "
            f"bool or None, got {reachable_on_default_branch!r}"
        )

    source_observation_id = params.get("source_observation_id")

    # An item whose closure_fidelity the caller does not declare folds to
    # DEFAULT_CLOSURE_FIDELITY ("verify-with-effort") per
    # DR-closure-fidelity-tier-axis D4, which is the tier that can never
    # auto-assert. Defaulting here fails SAFE: the absent-input case degrades
    # to suggest rather than minting a false auto. Resolving the real value
    # from projected state stays the caller's job -- this module holds its
    # negative-spec import boundary and does not read the store to find it.
    closure_fidelity = params.get("closure_fidelity", DEFAULT_CLOSURE_FIDELITY)
    if closure_fidelity not in CLOSURE_FIDELITY_VALUES:
        raise ValueError(
            "tracker.assert_code_complete: closure_fidelity must be one of "
            f"{sorted(CLOSURE_FIDELITY_VALUES)}, got {closure_fidelity!r}"
        )

    evidence = CodeCompleteEvidence(
        sha=sha,
        trailer_bound=trailer_bound,
        reachable_on_default_branch=reachable_on_default_branch,
    )

    event = await asyncio.to_thread(
        emit_code_complete_assert,
        item_id,
        evidence,
        actor=actor,
        source_observation_id=source_observation_id,
        closure_fidelity=closure_fidelity,
        repo_root=worktree,
    )
    return event
