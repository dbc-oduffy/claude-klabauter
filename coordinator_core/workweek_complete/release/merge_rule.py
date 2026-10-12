"""Merge-evidence rule for workweek-complete: five checks on one commit, the stamp commit.

The candidate is a commit. `HEAD` is read only as the branch head in check 4; no `git status`
child is started, so a moved head and a peer's uncommitted edit are outside what is tested.

Every git child is a read from an argv list through `git.run.run_git`: at most eight (review
ancestor test, `diff-tree`, `rev-parse` of the parent, the range diff, two for the declared
version, branch ancestor test, `merge-tree`). The review dimension starts its own `git log`.
`merge-tree --write-tree` adds unreachable tree objects to the object store and moves no ref.
The review dimension is called alone through `gate_validate_invocable._run_dimension`; per
`gate_dimension_review.py` it reads git and the receipt store and writes nothing. Not
`_gate_validate_invocable`: that op is classed MUTATING and runs all five dimensions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from coordinator_core.coverage import _is_bookkeeping_path
from coordinator_core.git.run import run_git
from coordinator_core.ops.gate_validate_invocable import Verdict, _run_dimension
from coordinator_core.workweek_complete.release.contract import (
    GATE_IDS,
    GateRecord,
    RuleResult,
    row_field,
)
from coordinator_core.workweek_complete.release.declared import declared_version

_RULE = "merge-evidence"
_PATH_LIMIT = 20
_BAD_LEDGER_STATUSES = ("failed", "halted-by-gate", "not-composed", "cadence-skipped")


def _fail(reason: str, evidence: Dict[str, object]) -> RuleResult:
    return RuleResult(_RULE, False, reason, evidence)


def _git(repo_root: Path, args: Sequence[str]):
    return run_git(["-C", str(repo_root), *args])


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    return _git(repo_root, ["merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0


def _lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def _check_review(
    repo_root: Path,
    candidate: str,
    review_result: Any,
    stamped: frozenset[str],
    ev: Dict[str, object],
) -> Optional[RuleResult]:
    reviewed = row_field(review_result, "reviewed_sha") if review_result is not None else None
    ev["reviewed_sha"] = reviewed
    if not reviewed or not _is_ancestor(repo_root, str(reviewed), candidate):
        return _fail("review-missing", ev)
    if str(row_field(review_result, "verdict")).upper() == "BLOCKED":
        return _fail("review-blocked", ev)

    tree = _git(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-r", candidate])
    unreviewed = [
        p for p in _lines(tree.stdout) if not _is_bookkeeping_path(p) and p not in stamped
    ]
    if tree.returncode != 0 or unreviewed:
        ev["unreviewed_paths"] = unreviewed[:_PATH_LIMIT]
        return _fail("unreviewed-tail", ev)

    parent = _git(repo_root, ["rev-parse", f"{candidate}^"])
    parent_sha = parent.stdout.strip()
    if parent.returncode != 0 or not parent_sha:
        ev["dimension_detail"] = "candidate has no first parent"
        return _fail("review-uncovered", ev)
    diff_base = f"origin/main..{parent_sha}"
    changed = _git(repo_root, ["diff", "--name-only", diff_base])
    result = _run_dimension("review", _lines(changed.stdout), diff_base, repo_root)
    ev["diff_base"] = diff_base
    ev["dimension_verdict"] = result.verdict.value
    ev["dimension_detail"] = result.detail
    if changed.returncode != 0 or result.verdict is not Verdict.PASS:
        return _fail("review-uncovered", ev)
    return None


def _check_gates(
    candidate: str, gate_records: Iterable[GateRecord], ev: Dict[str, object]
) -> Optional[RuleResult]:
    by_gate = {r.gate: r for r in gate_records}
    for gate in GATE_IDS:
        rec = by_gate.get(gate)
        if rec is None or rec.status == "not-started":
            ev["gate"] = gate
            return _fail("gate-tests-missing", ev)
        if rec.tested_sha != candidate:
            ev["gate"], ev["tested_sha"] = gate, rec.tested_sha
            return _fail("gate-tests-other-commit", ev)
        if rec.status == "not-present":
            if gate != "d0":
                ev["gate"] = gate
                return _fail("gate-tests-missing", ev)
            continue
        if rec.exit_code != 0:
            ev["gate"], ev["exit_code"] = gate, rec.exit_code
            return _fail("gate-tests-failed", ev)
    return None


def evaluate(
    repo_root: Path,
    *,
    candidate: str,
    version: str,
    review_result: Any,
    gate_records: Iterable[GateRecord],
    ledger_rows: Iterable[object],
    project_subtypes: Iterable[str],
    stamped_paths: Iterable[str],
    branch_head: str = "HEAD",
) -> RuleResult:
    """Rule `merge-evidence`. Checks run in order 0..5; the first miss returns, and `evidence`
    holds every check reached. `candidate` is the stamp commit; `gate_records` the gate run's
    `GateRecord` tuple; `stamped_paths` the version surfaces plus the notes file."""
    repo_root = Path(repo_root)
    ev: Dict[str, object] = {"candidate": candidate, "version": version}

    if "unreal" in tuple(project_subtypes):
        return _fail("project-checks-not-composed", ev)

    miss = _check_review(repo_root, candidate, review_result, frozenset(stamped_paths), ev)
    if miss is not None:
        return miss
    ev["review"] = "covered"

    miss = _check_gates(candidate, tuple(gate_records), ev)
    if miss is not None:
        return miss
    ev["gates"] = "passed"

    bad: Tuple[str, ...] = tuple(
        str(row_field(r, "id"))
        for r in ledger_rows
        if row_field(r, "status") in _BAD_LEDGER_STATUSES
    )
    if bad:
        ev["steps"] = list(bad)
        return _fail("ledger-not-clean", ev)
    ev["ledger"] = "clean"

    declared = declared_version(repo_root, treeish=candidate)
    ev["declared_version"] = declared
    if declared != version:
        return _fail("not-stamped", ev)
    if not _is_ancestor(repo_root, candidate, branch_head):
        ev["branch_head"] = branch_head
        return _fail("candidate-not-on-branch", ev)

    merged = _git(repo_root, ["merge-tree", "--write-tree", "--name-only", "origin/main", candidate])
    if merged.returncode != 0:
        # First line is the tree oid, then conflicted paths up to a blank line.
        paths: list[str] = []
        for ln in merged.stdout.splitlines()[1:]:
            if not ln.strip():
                break
            paths.append(ln)
        ev["conflicted_paths"] = paths[:_PATH_LIMIT]
        ev["merge_tree_rc"] = merged.returncode
        return _fail("main-conflict", ev)
    ev["main"] = "merges-clean"
    return RuleResult(_RULE, True, evidence=ev)
