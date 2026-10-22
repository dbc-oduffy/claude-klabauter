"""The workstream-complete brief's `gates.run_close`: which directives a run (or a run-less
entry) already paid, stamped as `discharged_by` on each directive.

The directive-to-leg map is engine data. Lessons and the orientation pinboard carry
EM-authored args a run cannot have paid, so they are absent from the map and never
discharge. Imports stay lazy so the brief's cold path pays nothing for a session with no
close.
"""

from __future__ import annotations

import posixpath
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

WSC_DIRECTIVE_LEGS: dict[str, str] = {
    "d-run-review-brightline-gate": "review",
    "d-close-coverage-advisory": "review",
    "d-claim-plan-execution-lock": "plan-reconciliation",
    "d-stamp-plan-implemented": "plan-reconciliation",
    "d-fold-execution-observations": "plan-reconciliation",
    "d-complete-entry": "completion-entry",
    "d-sweep-terminal-handoffs": "baton-drain",
    "d-sweep-terminal-sizings": "baton-drain",
    "d-baton-chain-closure": "post-close",
    "d-structural-index-refresh": "post-close",
    "d-ceremony-reindex": "post-close",
    "d-check-machine-local-regeneratability": "post-close",
}

WSC_DIRECTIVE_PREFIX_LEGS: tuple[tuple[str, str], ...] = (
    ("d-harvest-deferrals-", "plan-reconciliation"),
    ("d-ship-consumed-handoff:", "baton-ship"),
    ("d-claim-memo-stamp:", "memo-lifecycle"),
    ("d-flip-memo-status:", "memo-lifecycle"),
)

EM_AUTHORED_PREFIXES: tuple[str, ...] = ("d-add-lesson-", "d-queue-append-lesson-")
EM_AUTHORED_IDS: frozenset[str] = frozenset({"d-append-orientation-pinboard"})

_SHIP_PREFIX = "d-ship-consumed-handoff:"
_BIRTH_SLACK_SECONDS = 60.0


def leg_of(directive: dict[str, Any]) -> Optional[str]:
    """The run-bound leg that pays *directive*, or ``None`` when no run can have paid it."""
    did = str(directive.get("id") or "")
    if did in EM_AUTHORED_IDS or did.startswith(EM_AUTHORED_PREFIXES):
        return None
    leg = WSC_DIRECTIVE_LEGS.get(did)
    if leg is not None:
        return leg
    for prefix, prefix_leg in WSC_DIRECTIVE_PREFIX_LEGS:
        if did.startswith(prefix):
            return prefix_leg
    return None


def _suffix(directive: dict[str, Any], prefix: str) -> str:
    return str(directive.get("id") or "")[len(prefix):]


def _plan_facts(root: Path, plan_rel: Optional[str], cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """`{review_stamp: bool, status: str}` read from the plan's frontmatter; empty when unreadable."""
    if not plan_rel:
        return {}
    if plan_rel in cache:
        return cache[plan_rel]
    facts: dict[str, Any] = {}
    try:
        import yaml
        from coordinator_core.frontmatter.primitives import split_frontmatter

        text = (Path(root) / plan_rel).read_text(encoding="utf-8")
        split = split_frontmatter(text)
        fm = (yaml.safe_load(split.fm_text) or {}) if split is not None else {}
        if isinstance(fm, dict):
            facts = {"review_stamp": bool(fm.get("review_stamp")), "status": str(fm.get("status") or "")}
    except Exception:  # noqa: BLE001 - an unreadable plan proves nothing
        facts = {}
    cache[plan_rel] = facts
    return facts


def _entry_is_own(root: Path, sid: Optional[str], close: Any) -> bool:
    """True only when *close*'s completion entry is provably the closing session's own run's.

    A run-less close is keyed on an entry already verified ``authored_by == sid``. A plan-run
    entry must be readable and carry no ``run_id`` other than the close's and no
    ``authored_by`` other than *sid*; an unreadable entry proves nothing. A readable entry
    carrying neither field is accepted (not provably foreign).
    """
    entry = close.completion_entry
    if not entry:
        return False
    if entry == close.run_id:
        return True
    try:
        from coordinator_core.frontmatter.schema_validate import parse_frontmatter

        text = (Path(root) / entry).read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text).get("frontmatter") or {}
    except Exception:  # noqa: BLE001 - an unreadable entry proves nothing
        return False
    run_id, author = fm.get("run_id"), fm.get("authored_by")
    if run_id and str(run_id) != close.run_id:
        return False
    return not (author and str(author) != sid)


def session_birth_floor(root: Path, sid: str) -> Optional[float]:
    """Epoch seconds of the session claim dir's creation, or ``None`` when it cannot be read
    without a git spawn. A directory mtime is never used: it advances on every claim added."""
    from coordinator_core.ops.dispatch_emit.run_close import dir_birth_time

    try:
        from coordinator_core.session import core as _core

        sdir = _core.session_dir(sid, str(root))
    except (OSError, ValueError):
        return None
    birth = dir_birth_time(Path(sdir)) if sdir else None
    return None if birth is None else birth - _BIRTH_SLACK_SECONDS


def compute_run_close_gate(
    root: Path,
    sid: Optional[str],
    directives: list[dict[str, Any]],
    *,
    governing_plan_rel: Optional[str],
    since: Optional[float],
) -> dict[str, Any]:
    """Stamp ``discharged_by`` on every directive and return the ``gates.run_close`` payload.

    The closes are the session's close-job records plus its run-less completion entry
    (oldest first, so a plan-run close outranks the entry for ``completion-entry``).
    """
    from coordinator_core.ops.dispatch_emit import run_close

    closes: list[Any] = []
    unreadable: list[str] = []
    if sid:
        closes, unreadable = run_close.session_closes(
            Path(root), sid, datetime.now(timezone.utc), since
        )

    plan_cache: dict[str, dict[str, Any]] = {}
    ship_directives = [d for d in directives if str(d.get("id") or "").startswith(_SHIP_PREFIX)]

    def _paid(leg: str, close: Any) -> bool:
        result = (close.legs or {}).get(leg)
        return result is not None and result.status in run_close.DISCHARGING_STATUSES

    def _ship_eligible(directive: dict[str, Any], close: Any) -> bool:
        return _suffix(directive, _SHIP_PREFIX) in {posixpath.basename(b) for b in close.claimed_batons}

    def _ship_left_undischarged() -> bool:
        return any(
            not any(_paid("baton-ship", c) and _ship_eligible(s, c) for c in closes)
            for s in ship_directives
        )

    drain_blocked = _ship_left_undischarged()

    def eligible(directive: dict[str, Any], close: Any) -> bool:
        leg = leg_of(directive)
        if leg == "review":
            return bool(_plan_facts(root, close.review_receipt or close.plan_rel, plan_cache).get("review_stamp"))
        if leg == "plan-reconciliation":
            if not close.plan_rel or close.plan_rel != governing_plan_rel:
                return False
            return _plan_facts(root, close.plan_rel, plan_cache).get("status") == "implemented"
        if leg == "completion-entry":
            return _entry_is_own(root, sid, close)
        if leg == "baton-ship":
            return _ship_eligible(directive, close)
        if leg == "baton-drain":
            return not drain_blocked
        if leg == "memo-lifecycle":
            if not close.source_memo:
                return False
            did = str(directive.get("id") or "")
            prefix = next(p for p, pl in WSC_DIRECTIVE_PREFIX_LEGS if pl == "memo-lifecycle" and did.startswith(p))
            return _suffix(directive, prefix) == posixpath.basename(close.source_memo)
        return True

    return run_close.discharge(directives, leg_of, closes, eligible, unreadable)


def undischarged(directives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The directives no close paid, the set a judgment point must gate to still be asked."""
    from coordinator_core.ops.dispatch_emit.run_close import DISCHARGED_BY_KEY

    return [d for d in directives if d.get(DISCHARGED_BY_KEY) is None]
