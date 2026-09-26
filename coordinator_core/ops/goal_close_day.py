"""
coordinator_core.ops.goal_close_day — day-goal enumeration + close-out
(``goal.close_day`` read leg, ``goal.close_day_apply`` write leg).

Purpose: enumerate open (``status`` not in ``{done, dropped}``) ``period == "day"``
rows from the goals-log wire whose ``period_value`` is TODAY or EARLIER, scoped to
one ``(repo, coordinator_root_path)``, partitioned into "today" and "stale" buckets
(``goal.close_day``); and close a set of those rows by re-appending each at its SAME
``goal_id`` with a terminal ``done``/``dropped`` status (``goal.close_day_apply``).
Feeds the day-goal close-out ceremony — see
docs/plans/2026-07-25-day-goal-close-out-lifecycle.md § C2/C3.

The "<= today" widening (not a today-only predicate) is load-bearing: /workday-start
runs on days /workday-complete does not, so a today-only filter would make the plan's
close-out success condition unreachable and the stale open-goal backlog only grows.

Both legs consume the shared collapsed wire read from
``coordinator_core.goals.wire_read.read_and_collapse`` — this module does NOT glob
``goals-log.*.jsonl`` itself and does NOT re-implement the latest-wins collapse. That
collapse exists exactly once, in ``wire_read.py`` (see that module's docstring and
``ops/emit/sections/goals.py``'s own negative-spec against a second copy). The close
leg re-reads through the SAME collapse, as a runtime postcondition, after writing —
see ``close_day_goals()``.

Degrade posture (read leg only): an unreadable ``central_state_root``
(permission-denied, etc.) degrades to zero open rows rather than raising.
``wire_read.read_and_collapse`` is policy-neutral by design — the emit path
(``ops/emit/sections/goals.py``) treats an unreadable root as fatal
(``GoalsStateRootUnreadable``); ``goal.close_day`` instead feeds a ceremony
directive bound by a never-fail-the-ceremony rule, so the raise-vs-degrade choice
here is the opposite of the emit path's, deliberately. The write leg
(``close_day_goals()``) does NOT share this degrade posture — an unreadable root
or a lost supersession there fails loud (see that function's docstring), since a
silent write-side failure is exactly the defect class this op exists to close.

Registered as ``goal.close_day`` (COMPUTE_ONLY) and ``goal.close_day_apply``
(MUTATING) in ``ops/__init__.py``'s eager-import table, classified in
``authz/classification.py``, and scoped ``"common_dir"`` in ``op_scopes.py`` (same
key-scope class as ``goal.match_candidates`` — reads/writes state under the main
worktree resolved from the router-supplied ``git_common_dir``).

Negative-spec:
  - Does NOT glob ``goals-log.*.jsonl`` or re-derive the latest-wins collapse —
    delegates entirely to ``wire_read.read_and_collapse``.
  - ``goal.close_day`` does NOT write any file, issue any git command, or mutate
    any coordinator substrate.
  - ``goal.close_day`` does NOT filter to today's ``period_value`` only — returns
    every open day row with ``period_value <= today``, partitioned into
    today/stale buckets.
  - Neither leg crosses ``(repo, coordinator_root_path)`` scope boundaries — a row
    belonging to a different repo sharing the same on-disk shard is excluded.
  - ``close_day_goals()`` does NOT reimplement the append, the shard-path
    resolution, or the ``_goal_id`` content-hash formula — it calls
    ``coordinator_core.ops.goal_append.append_goal()`` for every write.
  - ``close_day_goals()`` does NOT edit, rewrite, migrate, or delete a prior wire
    row. The wire is append-only; closing is an append that supersedes via the
    latest-wins collapse.
  - ``close_day_goals()`` does NOT infer ``done`` vs ``dropped`` from commits, test
    runs, or changelog content — the caller's decision map is the only input.
  - ``close_day_goals()`` does NOT accept a decision naming an already-``done``/
    ``dropped`` goal_id — that resolves as not-found (same ``ValueError`` as an
    unknown goal_id), not as a silent re-close, since the wire's latest-wins
    collapse would otherwise let a stray re-close overwrite a terminal status.
  - An empty/absent ``decisions`` mapping returns before ``close_day_goals()``
    reads the wire at all (DEC-2) — the unreadable-root/not-open/lost-
    supersession fail-loud postures below only apply once there is at least one
    decision to act on.

Spec backlink: pln-day-scoped-goal-close-out-life-69a25c § C2/C3
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

from coordinator_core.goals.wire_read import read_and_collapse
from coordinator_core.ipc import register_op
from coordinator_core.ops.emit.resolvers import resolve_context
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.goal_append import append_goal

_DONE_LITERAL = "done"

_CLOSED_STATUSES = frozenset({"done", "dropped"})

_PASSTHROUGH_FIELDS = ("parent_goal_id", "key_results_status", "weekly_perceptible")


def _resolve_today(today_param: Optional[str]) -> date:
    if today_param:
        try:
            return date.fromisoformat(today_param)
        except ValueError:
            print(
                f"goal.close_day: unparseable today override {today_param!r} — "
                "falling back to the real UTC-today",
                file=sys.stderr,
            )
    return datetime.now(timezone.utc).date()


def _row_is_open(record: dict) -> bool:
    status = record.get("status") or "active"
    return status not in _CLOSED_STATUSES


def _project_row(goal_id: str, record: dict, *, default_repo: str = "") -> dict:
    row = {
        "goal_id": goal_id,
        "text": record.get("text", ""),
        "repo": record.get("repo", default_repo),
        "coordinator_root_path": record.get("coordinator_root_path", "."),
        "period": record.get("period", ""),
        "period_value": record.get("period_value", ""),
        "status": record.get("status") or "active",
    }
    for field in _PASSTHROUGH_FIELDS:
        if field in record:
            row[field] = record[field]
    return row


def collect_open_day_goals(
    central_state_root: Path,
    repo: str,
    *,
    coordinator_root_path: str = ".",
    today: Optional[str] = None,
    default_repo: str = "",
) -> dict:
    """Enumerate open ``period == "day"`` rows for ``(repo, coordinator_root_path)``
    with ``period_value <= today``, partitioned into today/stale buckets.

    Parameters:
        central_state_root     — directory scanned for ``goals-log.*.jsonl`` shards
                                 (passed straight through to
                                 ``wire_read.read_and_collapse``).
        repo                   — the requesting repo slug; only rows whose collapsed
                                 record carries this EXACT ``repo`` value are returned.
        coordinator_root_path  — the requesting coordinator root; only rows whose
                                 collapsed record carries this EXACT value are
                                 returned. Default ``"."`` (single-root repo).
        today                  — optional ISO ``YYYY-MM-DD`` override for the
                                 reference date (test seam); defaults to the real
                                 UTC-today.
        default_repo           — passed through to ``read_and_collapse`` for legacy
                                 rows with no ``repo`` field (matches its own default).

    Returns:
        {
            "today":  [<row>, ...],   # period_value == today, open, in-scope
            "stale":  [<row>, ...],   # period_value <  today, open, in-scope
            "unreadable_error": str | None,
        }

    Never raises: an unreadable ``central_state_root`` degrades to empty
    today/stale lists plus a non-null ``unreadable_error`` string, per this
    module's degrade posture (see module docstring).
    """
    result = read_and_collapse(Path(central_state_root), default_repo=default_repo)

    today_str = _resolve_today(today).isoformat()

    today_rows: List[dict] = []
    stale_rows: List[dict] = []

    for log_row in result.rows:
        record = log_row.record
        if record.get("period") != "day":
            continue
        if record.get("repo", default_repo) != repo:
            continue
        if record.get("coordinator_root_path", ".") != coordinator_root_path:
            continue
        period_value = record.get("period_value", "")
        if not period_value or period_value > today_str:
            continue
        if not _row_is_open(record):
            continue

        row = _project_row(log_row.goal_id, record, default_repo=default_repo)
        if period_value == today_str:
            today_rows.append(row)
        else:
            stale_rows.append(row)

    return {
        "today": today_rows,
        "stale": stale_rows,
        "unreadable_error": str(result.unreadable_error) if result.unreadable_error else None,
    }


class GoalCloseDayLostSupersession(RuntimeError):
    pass


class GoalCloseDayRootUnreadable(RuntimeError):
    pass


def _terminal_status(raw_decision: object) -> str:
    return _DONE_LITERAL if raw_decision == _DONE_LITERAL else "dropped"


def close_day_goals(
    central_state_root: Path,
    repo: str,
    decisions: Optional[dict],
    *,
    coordinator_root_path: str = ".",
    hostname: Optional[str] = None,
    default_repo: str = "",
) -> dict:
    if not decisions:
        return {"closed": []}

    before = read_and_collapse(Path(central_state_root), default_repo=default_repo)
    if before.unreadable_error is not None:
        raise GoalCloseDayRootUnreadable(
            f"goal.close_day_apply: {central_state_root!r} could not be scanned "
            f"({before.unreadable_error}) — refusing to close-out against an "
            "unreadable wire rather than silently treating every goal_id as "
            "not-found"
        )

    source_by_goal_id: dict = {}
    for log_row in before.rows:
        record = log_row.record
        if record.get("repo", default_repo) != repo:
            continue
        if record.get("coordinator_root_path", ".") != coordinator_root_path:
            continue
        if not _row_is_open(record):
            continue
        source_by_goal_id[log_row.goal_id] = record

    missing = [gid for gid in decisions if gid not in source_by_goal_id]
    if missing:
        raise ValueError(
            f"goal.close_day_apply: no open in-scope wire row for goal_id(s) "
            f"{missing!r} under (repo={repo!r}, coordinator_root_path="
            f"{coordinator_root_path!r}) — nothing to close"
        )

    closed: List[dict] = []
    for goal_id, raw_decision in decisions.items():
        source = source_by_goal_id[goal_id]
        terminal_status = _terminal_status(raw_decision)
        summary = append_goal(
            period=source.get("period", ""),
            period_value=source.get("period_value", ""),
            text=source.get("text", ""),
            repo=source.get("repo", default_repo),
            coordinator_root_path=source.get("coordinator_root_path", "."),
            hostname=hostname,
            central_state_root=Path(central_state_root),
            key_results_status=source.get("key_results_status"),
            weekly_perceptible=source.get("weekly_perceptible"),
            parent_goal_id=source.get("parent_goal_id"),
            status=terminal_status,
            goal_id=goal_id,
        )
        closed.append(
            {
                "goal_id": goal_id,
                "status": terminal_status,
                "log_file": summary["log_file"],
            }
        )

    after = read_and_collapse(Path(central_state_root), default_repo=default_repo)
    after_status_by_goal_id = {
        log_row.goal_id: log_row.record.get("status")
        for log_row in after.rows
        if log_row.record.get("repo", default_repo) == repo
        and log_row.record.get("coordinator_root_path", ".") == coordinator_root_path
    }
    lost = [
        entry["goal_id"]
        for entry in closed
        if after_status_by_goal_id.get(entry["goal_id"]) != entry["status"]
    ]
    if lost:
        raise GoalCloseDayLostSupersession(
            f"goal.close_day_apply: {len(lost)} close-out append(s) did not win the "
            f"latest-wins collapse (goal_id(s) {lost!r}) — likely a same-second tie "
            "or cross-machine clock skew; the row(s) remain open on the wire despite "
            "the append landing on disk"
        )

    return {"closed": closed}


# JSON-RPC handlers


@register_op("goal.close_day")
def _goal_close_day(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'goal.close_day' handler — read-only open-day-goal enumeration.

    COMPUTE_ONLY (reads the collapsed goals-log wire, returns a computed
    today/stale partition; writes nothing).

    repo_root (injected by ipc.dispatch_message): git_common_dir of the originating
    worktree. Mirrors ``goal.append``'s handler: derives the main-worktree root via
    ``main_worktree_root(repo_root)`` then builds an ``EmitContext`` from it for
    ``central_state_root``/``repo_name`` resolution — never the ambient-cwd
    param-less ``resolve_context()`` fallback.

    Optional params:
        coordinator_root_path (str) — coordinator root scoped by this call
                                      (default ".").
        today                  (str) — ISO ``YYYY-MM-DD`` reference-date override
                                      (test seam); default real UTC-today.
        repo                   (str) — repo slug override (default: resolved from
                                      per-repo context, same precedence as
                                      ``goal.append``).

    Returns: {"today": [...], "stale": [...], "unreadable_error": str | None}.
    Degrades to empty lists on an unreadable central_state_root (never raises) —
    see module docstring.
    """
    if repo_root is None:
        return {"today": [], "stale": [], "unreadable_error": "repo_root not resolved"}

    derived_root = main_worktree_root(repo_root)
    ctx = resolve_context(derived_root)

    return collect_open_day_goals(
        ctx.central_state_root,
        params.get("repo") or ctx.repo_name,
        coordinator_root_path=params.get("coordinator_root_path", "."),
        today=params.get("today"),
        default_repo=ctx.repo_name,
    )


@register_op("goal.close_day_apply")
def _goal_close_day_apply(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'goal.close_day_apply' handler — close-out write leg.

    MUTATING (re-appends one row per decision at its SAME goal_id via
    ``goal_append.append_goal``; never edits a prior row). Delegates to
    ``close_day_goals()`` — see that function's docstring for the full contract,
    including the runtime-postcondition fail-loud behaviour on a lost
    supersession.

    repo_root (injected by ipc.dispatch_message): git_common_dir of the
    originating worktree. Mirrors ``goal.close_day``/``goal.append``: derives the
    main-worktree root via ``main_worktree_root(repo_root)`` then builds an
    ``EmitContext`` from it for ``central_state_root``/``repo_name`` resolution —
    never the ambient-cwd param-less ``resolve_context()`` fallback.

    Required params:
        decisions (dict) — {goal_id: raw_decision}. Empty/absent writes NOTHING
                           (DEC-2) — see ``close_day_goals()``.

    Optional params:
        coordinator_root_path (str) — coordinator root scoped by this call
                                      (default ".").
        repo                   (str) — repo slug override (default: resolved from
                                      per-repo context, same precedence as
                                      ``goal.append``/``goal.close_day``).

    Returns: {"closed": [{"goal_id", "status", "log_file"}, ...]}.

    Raises:
        ValueError: repo_root unresolved, or a decision names a goal_id with no
            open in-scope wire row.
        GoalCloseDayLostSupersession: a close-out append did not win the
            latest-wins collapse on re-read (fail loud — see module docstring's
            write-leg degrade posture).
    """
    if repo_root is None:
        raise ValueError(
            "goal.close_day_apply requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be "
            "'common_dir' and _origin_worktree must be present in the JSON-RPC "
            "envelope. No silent fallback to meta-repo."
        )

    derived_root = main_worktree_root(repo_root)
    ctx = resolve_context(derived_root)

    return close_day_goals(
        ctx.central_state_root,
        params.get("repo") or ctx.repo_name,
        params.get("decisions") or {},
        coordinator_root_path=params.get("coordinator_root_path", "."),
        default_repo=ctx.repo_name,
    )
