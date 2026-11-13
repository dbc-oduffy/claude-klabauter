"""coordinator_core.install.authoring_checkout_freshness — pure day-branch freshness interface.

No I/O: the decide table for an authoring checkout against its local
`refs/remotes/origin/main`, the roster filter over the repo registry and the
advisory line. `authoring_checkout_freshness_apply` (probes, fetch, ff-only)
builds on these names.
"""

from __future__ import annotations

from typing import Mapping, NamedTuple, Optional

ACTION_CURRENT = "current"
ACTION_FAST_FORWARD = "fast_forward"
ACTION_DEFERRED_LIVE_RUN = "deferred_live_run"
ACTION_BLOCKED_DIRTY_OVERLAP = "blocked_dirty_overlap"
ACTION_DIVERGED = "diverged"
ACTION_NO_MAIN = "no_main"
ACTION_DETACHED = "detached"

ACTIONS = (
    ACTION_CURRENT,
    ACTION_FAST_FORWARD,
    ACTION_DEFERRED_LIVE_RUN,
    ACTION_BLOCKED_DIRTY_OVERLAP,
    ACTION_DIVERGED,
    ACTION_NO_MAIN,
    ACTION_DETACHED,
)

#: Registry key of the published mirror; kept current by `klabauter_freshness`, not this module.
KLABAUTER_REGISTRY_KEY = "repos.claude_klabauter"

#: Overlapping paths named in a reason before the rest collapse to a count.
MAX_NAMED_PATHS = 10


class CheckoutLag(NamedTuple):
    """One checkout's position against `refs/remotes/origin/main`.

    `branch` is "" for a detached HEAD; `main_sha` is "" when the ref is absent.
    `ahead`/`behind` count HEAD-only / main-only commits.
    """

    key: str
    root: str
    branch: str
    head_sha: str
    main_sha: str
    ahead: int
    behind: int


class Verdict(NamedTuple):
    """`action` is one of `ACTIONS`; `reason` names the exact blocker."""

    action: str
    reason: str


def _named_paths(paths: tuple[str, ...]) -> str:
    shown = ", ".join(paths[:MAX_NAMED_PATHS])
    extra = len(paths) - MAX_NAMED_PATHS
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def decide(
    lag: CheckoutLag,
    overlap_paths: tuple[str, ...],
    live_run_ids: tuple[str, ...],
) -> Verdict:
    """Verdict for a checkout against local origin/main. Pure; never refuses.

    Order: detached, no_main, current (not behind, even when dirty or ahead),
    diverged (ahead > 0), deferred_live_run, blocked_dirty_overlap, fast_forward.
    `overlap_paths` are dirty tracked paths the update would also change; a dirty
    path outside the update never blocks. A caller with an unreadable run registry
    passes a sentinel id so the tree defers.
    """
    if not lag.branch:
        return Verdict(ACTION_DETACHED, "detached HEAD; never fast-forwarded")
    if not lag.main_sha:
        return Verdict(ACTION_NO_MAIN, "no refs/remotes/origin/main")
    if lag.behind <= 0:
        return Verdict(ACTION_CURRENT, "checkout not behind origin/main")
    if lag.ahead > 0:
        return Verdict(
            ACTION_DIVERGED,
            f"diverged from origin/main: ahead {lag.ahead}, behind {lag.behind}",
        )
    if live_run_ids:
        return Verdict(
            ACTION_DEFERRED_LIVE_RUN,
            f"behind origin/main by {lag.behind}; live engine run(s) writing in tree: "
            + ", ".join(live_run_ids),
        )
    if overlap_paths:
        return Verdict(
            ACTION_BLOCKED_DIRTY_OVERLAP,
            f"behind origin/main by {lag.behind}; {len(overlap_paths)} dirty path(s) "
            f"the update touches: {_named_paths(overlap_paths)}",
        )
    return Verdict(ACTION_FAST_FORWARD, f"behind origin/main by {lag.behind}, safe to fast-forward")


def authoring_roster(registry: Mapping[str, str]) -> list[tuple[str, str]]:
    """`(key, root)` for every registry entry except the klabauter mirror, sorted by key."""
    return sorted(
        (key, root) for key, root in registry.items() if key != KLABAUTER_REGISTRY_KEY
    )


def advisory_line(hostname: str, row: Mapping[str, object]) -> Optional[str]:
    """One advisory line for a result row; `None` for current and for a fast-forward that moved.

    A `fast_forward` row whose `moved` is falsy (the ff-only merge refused) is
    advisory. `row` carries `key`, `action`, `reason` and optionally `moved`,
    `fetch_error`.
    """
    action = row.get("action")
    if action == ACTION_CURRENT:
        return None
    if action == ACTION_FAST_FORWARD and row.get("moved"):
        return None
    line = f"{hostname}: {row.get('key')}: {action}: {row.get('reason')}"
    fetch_error = row.get("fetch_error")
    if fetch_error:
        line += f" [{fetch_error}]"
    return line
