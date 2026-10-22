"""plan.census_order: orders a plan set by the engine's measured census cost, from memory alone.

Contract (COMPUTE_ONLY, scope `common_dir`). Wire params: `plans` (list[str], required),
repo-relative plan paths in the caller's order. Reply:

    {"plans": [rel, ...],                  # the same paths, reordered
     "estimated": int,                     # how many had a CensusSlot
     "estimates_ms": {rel: int | None}}    # None for a plan with no slot

The op reads `plan_mise_prep_probe._CENSUS_MEMO` ({(root, plan_rel): CensusSlot}) and nothing else.
estimate(plan) = sum over slot.spawning of memo[c].key_ms when memo[c].key is not None (an expected
hit at an unchanged tree), else memo[c].run_ms when measured, else M. M is the median run_ms over
every MemoEntry of this root, or 1 when none is measured. Order: ascending estimate; ties go to the
plan with more never-judged spawning commands (most likely to drift), then input order. A plan with
no slot, or a slot not of the pinned shape, goes last in input order, so a fresh engine returns
input order unchanged.

DR-208 five-question affirmation (citing ops/plan_mise_prep_probe.py):
  1. Writes, deletes, or reorders any state file, queue, or git object?  No.
  2. Writes into rag's relational store?                                 No.
  3. Opens any file for write (including sentinel creation)?             No.
  4. Mutates shared mutable state outside its own module?                No.
  5. Persistent state changes observable across process boundaries?      No.

Negative-spec: opens no file, spawns no process, runs no git; never mutates `_CENSUS_MEMO`.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops import plan_mise_prep_probe

MUTATES: list = []
GENERATES: list = []

_SLOT_FIELDS = ("memo", "spawning", "judged_at")


def _is_slot(slot: Any) -> bool:
    return (
        isinstance(slot, tuple)
        and all(hasattr(slot, f) for f in _SLOT_FIELDS)
        and isinstance(slot.memo, dict)
        and isinstance(slot.spawning, tuple)
    )


def _measured(entry: Any) -> Optional[int]:
    run_ms = getattr(entry, "run_ms", None)
    return run_ms if isinstance(run_ms, (int, float)) and not isinstance(run_ms, bool) else None


def _median_run_ms(slots: list) -> float:
    runs = [
        r for slot in slots for e in slot.memo.values() if (r := _measured(e)) is not None
    ]
    return statistics.median(runs) if runs else 1


def _estimate(slot: Any, median: float) -> tuple[int, int]:
    total = 0.0
    never = 0
    for command in slot.spawning:
        entry = slot.memo.get(command)
        if entry is None:
            never += 1
            total += median
            continue
        key_ms = getattr(entry, "key_ms", None)
        if getattr(entry, "key", None) is not None and isinstance(key_ms, (int, float)):
            total += key_ms
            continue
        run_ms = _measured(entry)
        total += run_ms if run_ms is not None else median
    return int(round(total)), never


@register_op("plan.census_order")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.census_order" handler. See module docstring."""
    plans = params.get("plans")
    if not isinstance(plans, list) or not all(isinstance(p, str) for p in plans):
        raise ValueError("plans must be a list of repo-relative plan path strings")
    if repo_root is None:
        raise ValueError("plan.census_order requires a resolved repo_root")
    root = str(main_worktree_root(repo_root))

    memo = plan_mise_prep_probe._CENSUS_MEMO
    slots = {
        p: s for p in plans if _is_slot(s := memo.get((root, p)))
    }
    median = _median_run_ms(
        [s for (r, _p), s in list(memo.items()) if r == root and _is_slot(s)]
    )
    estimates = {p: _estimate(s, median) for p, s in slots.items()}

    ranked = sorted(
        (i for i, p in enumerate(plans) if p in estimates),
        key=lambda i: (estimates[plans[i]][0], -estimates[plans[i]][1], i),
    )
    unslotted = [i for i, p in enumerate(plans) if p not in estimates]
    return {
        "plans": [plans[i] for i in ranked + unslotted],
        "estimated": len(ranked),
        "estimates_ms": {p: (estimates[p][0] if p in estimates else None) for p in plans},
    }
