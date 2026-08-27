"""
coordinator_core.ops.tracker.render_status — tracker.render_status op.

Purpose: sat-06's registered read seam over `coordinator_core.tracker_projection.
render_status` — the consumption-side counterpart to `tracker.advance_status` /
`tracker.fold_observed_set` / `tracker.mint_person`. `tracker_projection.py`'s own
module docstring negative-spec explicitly deferred this registration to sat-06
("The read seam belongs to sat-06, which already names it") rather than
registering it itself as COMPUTE_ONLY — see that module's docstring for the
refuted-draft history and the exact guard citation.

Classification ruling (C2, DR-241 amendment dated 2026-08-20): MUTATING, not
COMPUTE_ONLY. No live claude-klabauter-internal consumer of `render_status` exists at
HEAD — only the benchmark harness, `test_tracker_projection`, and assertions
inside `test_tracker_completion_policy` (which does not itself call
`render_status`) reference it. `coordinator_core/tests/`'s per-op affirmation
guard (`TestAffirmationEraBoundedRegistrationGuard::
test_tracker_ops_are_classified_mutating_not_compute_only`) asserts every
`OP_CLASSIFICATION` key beginning `tracker.` is `OpClass.MUTATING` by
construction — this op's classification is conservative-by-construction, not
descriptive of this one op's own read-only handler body. See
`coordinator_core/authz/classification.py`'s own comment above this op's
`OP_CLASSIFICATION` entry for the ruling text, and this module's own negative-
spec below for what a future editor must NOT do about it.

This module reaches the projection ONLY through
`coordinator_core.tracker_projection.render_status` — never the underlying
sovereign-tracker append/read module directly, and never a hand-built
`state/sovereign-tracker/` path literal. `tracker_projection.py` is itself a
DR-241-affirmed referencer of that underlying module; this op inherits that
affirmation for the read it performs rather than re-deriving it. This module
does not itself write anything — no `emit_*` call, no `tracker_holder.
write_root_for` call, no file open for write anywhere in its own handler code.

Spec backlink: docs/plans/2026-08-18-sat-06-cockpit-consumption-seam.md
  § Tasks C3, § Acceptance Criteria AC3/AC4/AC5.
Spec backlink: docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
  § Amendment (2026-08-20) — the MUTATING ruling this op's registration discharges.

Negative-spec — a future editor must NOT:
  - Reclassify this op to `OpClass.COMPUTE_ONLY` without a fresh DR-241
    amendment naming a live claude-klabauter-internal consumer (C2's ruling; a
    benchmark harness or test file alone does not satisfy that bar).
  - Widen the DR-241-affirmed allowlist of sanctioned referencers of the
    underlying sovereign-tracker event-store module to admit this module.
    This module imports from `tracker_projection`, never the underlying
    store module directly, and must never need that widening.
  - Import the underlying sovereign-tracker append/read module directly, or
    hand-build a `state/`/`archive/` path literal.
  - Rename this op outside the `tracker.` prefix to evade the MUTATING guard
    cited above.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.tracker_projection import render_status

# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("tracker.render_status")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """tracker.render_status — render one item's computed open/closed status
    (see `coordinator_core.tracker_projection.render_status` for the fold this
    delegates to). See module docstring for the full contract.

    Wire contract:
        params: {item_id: str}. An optional params.repo_root is a D3
                 consistency check only (contract §3.3 doctrine) — never the
                 path source.
        ->      {"item_id": str, "status": "open" | "closed"}. On a D3
                 mismatch: raises ValueError (no partial/ambiguous envelope —
                 there is no honest "skipped" status to report for a read).

    `repo_root` handler arg is the git common dir (`_OP_KEY_SCOPE:
    "common_dir"`); the worktree root is derived via
    `main_worktree_root(repo_root)` — never from `params.repo_root`.

    Raises:
        RuntimeError — repo_root is None (engine misconfiguration; production
            always supplies it via `_OP_KEY_SCOPE='common_dir'`).
        ValueError — item_id missing/malformed, or a D3 `repo_root` mismatch.
    """
    if repo_root is None:
        raise RuntimeError(
            "tracker.render_status: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production; "
            "test fixtures must supply an explicit value"
        )
    common_dir = Path(repo_root)
    worktree = main_worktree_root(common_dir)

    # D3: optional repo_root consistency check (contract §3.3 doctrine).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        raise ValueError(f"tracker.render_status: {mismatch}")

    item_id = params.get("item_id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError(
            f"tracker.render_status: item_id must be a non-empty string, got {item_id!r}"
        )

    status = await asyncio.to_thread(render_status, item_id, repo_root=worktree)
    return {"item_id": item_id, "status": status}
