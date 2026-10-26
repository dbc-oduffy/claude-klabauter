"""Run-ownership predicate: may this run's commit or revert touch a path, given the other runs' state.

Pure and git-free: the caller passes every run's plan, withheld set and claims in. Consumed by the
halt-record and landing commit, review, and reverify so the three share one answer. A blocking verdict
maps to ``refused='peer-hunks-co-declared'`` naming the path and its release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

OWN = "own"
FOREIGN_WITHHELD = "foreign-withheld"
FOREIGN_CLAIM = "foreign-claim"
RELEASED_CLAIM = "released-claim"

BLOCKING_VERDICTS = frozenset({FOREIGN_WITHHELD, FOREIGN_CLAIM})


@dataclass(frozen=True)
class LiveRun:
    """One run's state; `withheld` and `claims` are repo-relative paths (a directory covers its children).

    Halted or held status is deliberately not a field: it never releases a claim. `superseded_by` is the
    run id that replaced this one; `successor_landed` is True once that successor has landed. `plan` is
    the plan path ("" when unknown; an empty plan never matches another).
    """

    plan: str = ""
    superseded_by: str = ""
    successor_landed: bool = False
    withheld: frozenset[str] = frozenset()
    claims: frozenset[str] = frozenset()


def _norm(path: str) -> str:
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _covers(entry: str, path: str) -> bool:
    e = _norm(entry).rstrip("/")
    return bool(e) and (path == e or path.startswith(e + "/"))


def _hit(entries: Iterable[str], path: str) -> bool:
    return any(_covers(e, path) for e in entries)


def _releases(run_id: str, run_plan: str, other: LiveRun) -> bool:
    """True when `other`'s hold on a path is released to `run_id`: its own superseded predecessor, a
    same-plan sibling, or a peer whose successor has landed. A peer on a different plan is never released."""
    if run_id and other.superseded_by == run_id:
        return True
    if run_plan and other.plan == run_plan:
        return True
    return other.successor_landed


def owns_path(run_id: str, path: str, live_runs: Mapping[str, LiveRun], run_plan: str = "") -> str:
    """Return own, foreign-withheld, foreign-claim or released-claim for `path` as seen by `run_id`.

    `run_plan` defaults to `live_runs[run_id].plan`. Precedence over the other runs: an unreleased run's
    withheld set, then its claim, then a released run's claim or withheld set. Only the first two block. Halted or held
    status never releases: a peer on a different plan stays foreign-withheld / foreign-claim. `run_id`
    itself is never foreign.
    """
    p = _norm(path)
    if not run_plan and run_id in live_runs:
        run_plan = live_runs[run_id].plan
    released_hit = False
    claim_hit = False
    for rid, other in live_runs.items():
        if rid == run_id:
            continue
        if _releases(run_id, run_plan, other):
            released_hit = released_hit or _hit(other.claims, p) or _hit(other.withheld, p)
            continue
        if _hit(other.withheld, p):
            return FOREIGN_WITHHELD
        claim_hit = claim_hit or _hit(other.claims, p)
    if claim_hit:
        return FOREIGN_CLAIM
    if released_hit:
        return RELEASED_CLAIM
    return OWN


def holders_from_held(held: Mapping[str, str]) -> dict[str, LiveRun]:
    """Turn `foreign_halted_withheld`'s `{path: holding run id}` into `{run id: LiveRun(withheld=...)}`.

    Each holder carries an unknown plan, so none of its paths is released to the caller. `{}` for empty input.
    """
    by_run: dict[str, set[str]] = {}
    for path, rid in held.items():
        if path and rid:
            by_run.setdefault(rid, set()).add(path)
    return {rid: LiveRun(withheld=frozenset(paths)) for rid, paths in by_run.items()}


def blocks(verdict: str) -> bool:
    """True when the verdict refuses the path."""
    return verdict in BLOCKING_VERDICTS
