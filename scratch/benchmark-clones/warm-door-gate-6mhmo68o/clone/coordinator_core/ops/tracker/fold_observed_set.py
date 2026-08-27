"""
coordinator_core.ops.tracker.fold_observed_set — tracker.fold_observed_set op.

Purpose: command-type entrypoint that actuates `coordinator_core.tracker_store.
fold_observed_set` (sat-01b C1) from `session.boot_sweep` (sat-01b C5), so a
machine's own sovereign tracker shard records what it has observed of its
peers on a cadence claude-klabauter already owns — no git hook, per the plan's "Why not
a git hook" section (hooks are not cloned with a repository at all, so a
hook-based actuator is silently absent on exactly the fresh machine that most
needs it).

This module is the SECOND (and, per the allowlist below, currently LAST)
registered referencer of `tracker_store` under `coordinator_core/ops/` —
`coordinator_core/tests/test_tracker_store.py`'s
`TestAffirmationEraBoundedRegistrationGuard` enumerates exactly two:
this file and `coordinator_core/ops/session/boot_sweep.py`. Adding a third
referencer anywhere under `coordinator_core/ops/` requires a fresh DR-241
five-bound affirmation against that new op's real handler code — widening the
guard's allowlist without one defeats the guard's purpose. That same guard
enforces write-target confinement by banning a hand-built `state/sovereign-
tracker/` path literal in EXECUTABLE code: this module reaches the store only
through `tracker_store.EVENTS_DIR_RELPATH`, never a duplicated literal.

OPT-IN BY EXISTENCE — the mandatory confinement gate (DEC-11, AC10b): this
op's handler runs the fold ONLY when `EVENTS_DIR_RELPATH` already exists as a
directory under the caller's repo root. `tracker_store.fold_observed_set`
itself also returns `None` on an absent store (belt and braces — see its own
docstring), but the handler checks first so it can report a distinct, honest
"skipped: no store" outcome rather than an ambiguous "no-op" that looks
identical to an idempotent duplicate-fold no-op. `session.boot_sweep` runs
fleet-wide (`_OP_KEY_SCOPE: common_dir`); unconditional actuation would mint
this store in every repo in the fleet, contradicting DEC-11's confinement of
the store to the CONSUMING repo. This gate is what keeps this op consistent
with the confinement bound DR-241's Amendment affirms.

Classification (DR-208 §5 correction, per DR-241's Amendment): this op writes
files, so it answers YES to DR-208 §5's question 1 and is MUTATING by
construction — the COMPUTE_ONLY checklist does not apply to it at all. See
`coordinator_core/authz/classification.py`'s own comment above this op's
`OP_CLASSIFICATION` entry for the full justification (written standalone per
this plan's instruction not to lean on `tracker.advance_status`'s comment,
which explicitly disclaims being precedent for a differently-shaped
`tracker.*` op).

Spec backlink: pln-sat-01b-observed-set-fold-actu-8b3f7a
  § Tasks C5, § Acceptance Criteria AC10/AC10b/AC14.
Spec backlink: docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
  § Amendment (2026-07-28) — the five-bound affirmation this op's
  registration discharges.

Negative-spec — a future editor must NOT:
  - Mint the `EVENTS_DIR_RELPATH` directory when it does not already exist.
    This op is opt-in-by-existence only (see above); creating the directory
    here would actuate the store fleet-wide the first time
    `session.boot_sweep` runs in any repo, contradicting DEC-11.
  - Read `params.repo_root` as the path source. `repo_root` is the git common
    dir (`_OP_KEY_SCOPE: "common_dir"`); the worktree root is derived via
    `main_worktree_root(repo_root)`, and `params.repo_root` (when supplied) is
    a D3 consistency check only, per this repo's contract §3.3 doctrine (see
    `coordinator_core/ops/session/boot_sweep.py`'s own handler docstring, the
    reference this module follows).
  - Reach across repos, or fall back to a claude-klabauter-tree default path. Writes
    land in the CONSUMING repo's own `EVENTS_DIR_RELPATH` directory only.
  - Add a peer-shard read to `append_event`, or hand-build a quoted store
    path string literal instead of importing `tracker_store.
    EVENTS_DIR_RELPATH` — DR-241's write-target confinement bound requires
    the sanctioned write target be reached ONLY through `tracker_store`'s own
    API/constant
    (`coordinator_core/tests/test_tracker_store.py`'s
    `test_allowlisted_referencers_confine_writes_via_tracker_store_api_only`
    enforces this).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.tracker_store import (
    EVENTS_DIR_RELPATH,
    EVENTS_SHARD_GLOB,
    fold_observed_set,
)


def run_fold_observed_set(*, repo_root: Path) -> dict:
    """Pure, synchronous core: opt-in-by-existence gate, then delegate to
    `tracker_store.fold_observed_set`.

    Returns a structured result dict:
        {"ran": bool, "reason": str, "marker": dict | None}

    - `ran: False, reason: "no_store"` — `EVENTS_DIR_RELPATH` does not exist
      under *repo_root*; no fold attempted, nothing written, nothing minted.
    - `ran: True, reason: "no_op"` — the store exists but this exact
      `observed_set` was already recorded by a prior marker on this machine's
      own shard (`tracker_store.fold_observed_set` returned `None`).
    - `ran: True, reason: "appended"` — a new marker was appended;
      `marker` carries the assigned marker event dict.
    """
    shard_dir = repo_root / EVENTS_DIR_RELPATH
    if not shard_dir.is_dir():
        return {"ran": False, "reason": "no_store", "marker": None}

    marker = fold_observed_set(repo_root=repo_root)
    if marker is None:
        return {"ran": True, "reason": "no_op", "marker": None}
    return {"ran": True, "reason": "appended", "marker": marker}


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("tracker.fold_observed_set")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """tracker.fold_observed_set — actuate one observed-set fold against the
    calling machine's own sovereign tracker shard, opt-in-by-existence only.
    See module docstring for the full contract.

    Wire contract:
        params: {} (no caller-supplied params; the op is self-selecting, same
                 shape as session.boot_sweep's own sub-sweeps). An optional
                 params.repo_root is a D3 consistency check only (contract
                 §3.3 doctrine) — never the path source.
        ->      {"ran": bool, "reason": str, "marker": dict | None}. On a D3
                 mismatch: {"ran": False, "reason": <mismatch string from
                 check_repo_root>, "marker": None} — the same envelope shape
                 as the "no_store" disposition, fail-closed. This op's
                 envelope carries no exit_code field (unlike
                 session.boot_sweep / fleet.* ops), so the mismatch is
                 surfaced via "reason" rather than a distinct exit_code.

    `repo_root` handler arg is the git common dir (`_OP_KEY_SCOPE:
    "common_dir"`); the worktree root is derived via
    `main_worktree_root(repo_root)` — never from `params.repo_root`.

    Raises:
        RuntimeError — repo_root is None (engine misconfiguration; production
            always supplies it via `_OP_KEY_SCOPE='common_dir'`).
    """
    if repo_root is None:
        raise RuntimeError(
            "tracker.fold_observed_set: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production; "
            "test fixtures must supply an explicit value"
        )
    common_dir = Path(repo_root)
    worktree = main_worktree_root(common_dir)

    # D3: optional repo_root consistency check (contract §3.3 doctrine).
    # Fail-closed on a genuine mismatch — never silently proceed (see
    # coordinator_core/ops/fleet/_common.py:check_repo_root's own doctrine,
    # matched here by boot_sweep.py:1374-1376 and every
    # coordinator_core/ops/fleet/*.py handler's identical guard).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return {"ran": False, "reason": mismatch, "marker": None}

    return await asyncio.to_thread(run_fold_observed_set, repo_root=worktree)
