"""Shared goals-log wire reader — the ONLY glob+parse+latest-wins-collapse of
``goals-log.*.jsonl`` shards.

Context-free: takes a ``central_state_root`` path (and a default-repo fallback string)
rather than an ``EmitContext``, so it can be consumed from any caller (emit sections,
future close-out ops) without importing emit-plane types.

Policy-neutral by design: an unscannable ``central_state_root`` is reported back to the
caller as a structured error, never raised here. Whether an unscannable root should abort
loud (the emit path's ``GoalsStateRootUnreadable``) or degrade gracefully (a future
close-out consumer) is a per-caller policy decision that does not belong in a shared,
context-free reader — see ``coordinator_core/ops/emit/sections/goals.py`` for the emit
path's raise-on-unreadable policy.

Spec backlink: pln-day-scoped-goal-close-out-life-69a25c § C1
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GoalLogRow:
    """One collapsed goals-log row — the surviving record for a goal identity."""

    goal_id: str
    record: dict
    shard_path: Path


@dataclass(frozen=True)
class GoalsLogReadResult:
    """Result of a goals-log read+collapse pass.

    ``rows`` is empty and ``unreadable_error`` is set when ``central_state_root``
    exists but could not be scanned (permission-denied etc.) — the caller decides
    whether that is fatal or a degrade-to-empty condition; this reader never raises.
    """

    rows: list[GoalLogRow]
    unreadable_error: OSError | None


def read_and_collapse(central_state_root: Path, *, default_repo: str = "") -> GoalsLogReadResult:
    """Read every ``goals-log.*.jsonl`` shard under ``central_state_root`` and collapse
    to one surviving row per goal identity (latest-``declared_at``-wins).

    Goal identity is keyed on ``("goal_id", goal_id, repo, coordinator_root_path)``.
    ``goal_id`` is the row's explicit wire value when present; legacy rows (written
    before ``goal_append.py`` started stamping ``goal_id``) compute the SAME
    deterministic id the writer would have minted, via
    ``coordinator_core.ops.goal_append._goal_id`` (imported, never re-derived), so a
    legacy row and a later goal_id-bearing row for the same logical goal collapse as
    supersession instead of double-emitting across the migration boundary.

    Same-``declared_at`` ties resolve to whichever row was encountered first in
    shard-filename sort order (deterministic-but-arbitrary, not true "latest wins").
    """
    if central_state_root.is_dir():
        # Probe via os.scandir BEFORE trusting glob() — glob()'s selector silently
        # swallows PermissionError while walking, which would otherwise make a
        # permission-denied root indistinguishable from a genuinely empty one.
        try:
            with os.scandir(central_state_root) as it:
                next(iter(it), None)
        except OSError as exc:
            return GoalsLogReadResult(rows=[], unreadable_error=exc)

    lines: list[tuple[str, Path]] = []
    for log_path in sorted(central_state_root.glob("goals-log.*.jsonl")):
        try:
            lines.extend((line, log_path) for line in log_path.read_text().splitlines())
        except (OSError, UnicodeDecodeError):
            # An unreadable/undecodable individual shard is quarantined (skipped),
            # not fatal to the whole read — distinct from the root-level probe above.
            continue

    latest: dict[tuple, GoalLogRow] = {}
    for raw_line, log_path in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # Malformed line quarantined — skip, continue with the next line.
            continue
        # A syntactically valid JSON line that is not an object (e.g. a bare `5`,
        # `"oops"`, `[1,2,3]`, `null`) parses fine but has no `.get()`; without this
        # guard the next line raises AttributeError and crashes the whole read
        # instead of quarantining just this one line.
        if not isinstance(record, dict):
            continue
        goal_id = record.get("goal_id", "")
        if not goal_id:
            # Deferred (function-local, not module-scope): a module-scope import here
            # closes a real circular-import cycle — resolvers -> sections/goals ->
            # wire_read -> goal_append -> resolvers (goal_append imports
            # resolve_context from emit.resolvers at module scope). Do not hoist.
            from coordinator_core.ops.goal_append import _goal_id  # noqa: PLC0415

            goal_id = _goal_id(
                record.get("repo", default_repo),
                record.get("coordinator_root_path", "."),
                record.get("period", ""),
                record.get("period_value", ""),
                record.get("text", ""),
            )
        key = (
            "goal_id",
            goal_id,
            record.get("repo", default_repo),
            record.get("coordinator_root_path", "."),
        )
        # Strict `>` — two rows for the same key sharing an identical declared_at
        # tie-break to whichever was encountered first in `lines` (shard-filename
        # sort order), not by wall-clock time.
        if key not in latest or record.get("declared_at", "") > latest[key].record.get(
            "declared_at", ""
        ):
            latest[key] = GoalLogRow(goal_id=goal_id, record=record, shard_path=log_path)

    return GoalsLogReadResult(rows=list(latest.values()), unreadable_error=None)
