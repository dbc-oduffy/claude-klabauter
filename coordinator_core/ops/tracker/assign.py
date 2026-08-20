"""
coordinator_core.ops.tracker.assign — tracker.assign op.

Purpose: the first production caller of the `item_person` edge — a
registered op that writes an `item_person_added`/`item_person_retracted`
event through the EXISTING `coordinator_core.tracker_entities.
emit_item_person_added` / `emit_item_person_retracted` functions. This op
is a caller, not a reimplementation: role validation (`ITEM_PERSON_ROLES`),
the duplicate-triple refusal (AC9), `applied_at` stamping (DEC-19), and
event-id minting (DEC-20) all stay in `tracker_entities.py` exactly where
C2/C4 of sat-02 landed them.

Registration mirrors `tracker.mint_person`'s own registry seam exactly —
`@register_op`, `_registry_map.py`, `ops/__init__.py`, an
`authz/classification.py` MUTATING entry with its own justification
comment, and `op_scopes.py`'s `"common_dir"` scope. No parallel dispatch,
no hand-rolled lookup.

WRITE BOUND — same per-repo confinement `tracker.mint_person` carries
(DEC-11, PM ruling 2026-08-12). The `repo_root` this op's handler receives
is the LOCAL repo's own worktree root, derived via `main_worktree_root`
from the `common_dir` the engine supplies (`_OP_KEY_SCOPE='common_dir'`,
same scope `tracker.mint_person`/`tracker.fold_observed_set` use) — never
derived from `__file__`, never resolved against this repo's own tree on a
caller's behalf, and never a holder/peer repo's root.

`person_id` is the tracker's OWN internal person id — the id
`tracker.mint_person` mints and writes into the sovereign-tracker person
registry. It is NOT the `contributor_slug` a consumer joins on (that value
is carried on the artifact axis in C4 of the parent plan) — the two are
kept deliberately distinct: an internal id that never leaves this repo,
and a slug that does.

Spec backlink: state/dispatch-briefs/2026-08-19-the-tracker-names-an-owner/C2.md

Negative-spec — a future editor must NOT:
  - Reimplement role validation, the duplicate-triple refusal, `applied_at`
    stamping, or event-id minting here — all of it stays in
    `tracker_entities.py`; this op only calls
    `emit_item_person_added`/`emit_item_person_retracted`.
  - Derive `repo_root` from `__file__`, or resolve it against this repo's
    own tree instead of the caller-supplied `common_dir` (DEC-11).
  - Accept a `contributor_slug` as `person_id` — `person_id` is the
    tracker's internal person id only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.tracker_entities import (
    TrackerEntityError,
    emit_item_person_added,
    emit_item_person_retracted,
)


@register_op("tracker.assign")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """tracker.assign — write (or retract) an `item_person` edge through
    `tracker_entities.emit_item_person_added`/`emit_item_person_retracted`.

    Wire contract:
        params: {
            "item_id": str,
            "person_id": str | None,
            "role": str,
            "retract": bool (optional, default False),
            "repo_root": str (optional — D3 consistency check only),
        }
        ->      {"assigned": bool, "reason": str, "item_id", "person_id",
                 "role"}. On a D3 mismatch:
                 {"assigned": False, "reason": <mismatch string>,
                  "item_id": None, "person_id": None, "role": None}.
                 On a `TrackerEntityError` (invalid role, duplicate triple,
                 foreign-repo item), the error is allowed to propagate —
                 the same fail-loud contract `emit_item_person_added`/
                 `emit_item_person_retracted` already carry.

    `repo_root` handler arg is the git common dir (`_OP_KEY_SCOPE:
    "common_dir"`); the worktree root is derived via
    `main_worktree_root(repo_root)` — never from `params.repo_root`.

    Raises:
        RuntimeError — repo_root is None (engine misconfiguration; production
            always supplies it via `_OP_KEY_SCOPE='common_dir'`).
        TrackerEntityError — propagated from `tracker_entities` (invalid
            role, duplicate triple, foreign-repo item).
    """
    if repo_root is None:
        raise RuntimeError(
            "tracker.assign: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production; "
            "test fixtures must supply an explicit value"
        )
    common_dir = Path(repo_root)
    worktree = main_worktree_root(common_dir)

    # D3: optional repo_root consistency check (contract §3.3 doctrine).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return {
            "assigned": False,
            "reason": mismatch,
            "item_id": None,
            "person_id": None,
            "role": None,
        }

    item_id = params["item_id"]
    person_id = params.get("person_id")
    role = params["role"]
    retract = bool(params.get("retract", False))

    if retract:
        await asyncio.to_thread(
            emit_item_person_retracted, item_id, person_id, role, repo_root=worktree
        )
        return {
            "assigned": False,
            "reason": "retracted",
            "item_id": item_id,
            "person_id": person_id,
            "role": role,
        }

    await asyncio.to_thread(
        emit_item_person_added, item_id, person_id, role, repo_root=worktree
    )
    return {
        "assigned": True,
        "reason": "added",
        "item_id": item_id,
        "person_id": person_id,
        "role": role,
    }
