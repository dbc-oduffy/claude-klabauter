"""Run-ownership predicate: may this run's commit or revert touch a path, given the other runs' state.

Pure and git-free: the caller passes every run's plan, withheld set and claims in. Consumed by the
halt-record and landing commit, review, and reverify so the three share one answer. A blocking verdict
maps to ``refused='peer-hunks-co-declared'`` naming the path and its release.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
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


@lru_cache(maxsize=512)
def _index(entries: frozenset) -> frozenset:
    """The normalised, slash-stripped, non-empty entries of one set, built once per distinct set."""
    return frozenset(e for e in (_norm(x).rstrip("/") for x in entries) if e)


def _indexed(entries: Iterable[str]) -> frozenset:
    return _index(entries if isinstance(entries, frozenset) else frozenset(entries))


def _in_index(index: frozenset, path: str) -> bool:
    """`_covers` against every entry at once: `path` or one of its directory prefixes is an entry."""
    if path in index:
        return True
    i = path.rfind("/")
    while i > 0:
        if path[:i] in index:
            return True
        i = path.rfind("/", 0, i)
    return False


def _hit(entries: Iterable[str], path: str) -> bool:
    return _in_index(_indexed(entries), path)


def covers_any(entries: Iterable[str], paths: Iterable[str]) -> bool:
    """True when some entry equals, or is a directory over, some path (`_covers` over the cross product,
    one prefix lookup per path)."""
    index = _indexed(entries)
    return bool(index) and any(_in_index(index, c) for c in paths)


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


def foreign_withheld_holders(
    run_id: str, paths: Iterable[str], live_runs: Mapping[str, LiveRun], run_plan: str = "",
) -> dict[str, list[str]]:
    """`{path: [run ids]}` (sorted) of every unreleased other run whose withheld set covers the path: what
    `owns_path(run_id, path, {rid: live_runs[rid]})` answers `FOREIGN_WITHHELD` for, one prefix index per run
    instead of a call per (path, run). Paths no run holds are absent."""
    held = [
        (rid, _indexed(other.withheld)) for rid, other in sorted(live_runs.items())
        if rid != run_id and not _releases(run_id, run_plan, other)
    ]
    out: dict[str, list[str]] = {}
    for path in paths:
        p = _norm(path)
        owners = [rid for rid, index in held if index and _in_index(index, p)]
        if owners:
            out[path] = owners
    return out


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
