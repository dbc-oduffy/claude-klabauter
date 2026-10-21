"""Workday-start's deciders: the rule table the in-run resolver and the runner both read.

Point rules (`recommendation`, `memo-inbox`) take `(point, *, context)` and return a Decision or
None. Step 0 rules take the runner's `(step, result, *, repo_root, run_dir)` and return True
(resolved), False, or a string failure reason. No rule merges, resolves a conflict, archives or
deletes a branch, or commits a path other than the stale-branch deferral file.

Stale-branch deferral and the next day: the deferral commit is a fresh commit on the branch, so
the stale guard does not fire again until the branch goes quiet for two more days, and that
commit is the only reason the next day's Step 0 reaches its reconcile leg.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from coordinator_core.contract import apply_base
from coordinator_core.git.run import GitResult, run_git

from coordinator_core.ceremony_common.run import decide
from coordinator_core.ceremony_common.run.decide import RECOMMENDATION, recommendation
from coordinator_core.ceremony_common.run.runner import Hold
from coordinator_core.git.reconcile_manifest import OUTCOME_CONFLICT_OPEN

from .contract import DECISIONS_FILE, Decision

NO_ACTION_DISPOSITIONS: frozenset[str] = frozenset({"defer", "leave", "leave_as_is", "leave_for_now"})
MEMO_DISPOSITIONS: frozenset[str] = frozenset({"accept", "decline"})
ACTING_DISPOSITIONS: frozenset[str] = frozenset(
    {"repair_hooks_now", "reset_week", "update_in_place", "run_workday_start_now", "close_stubs", "pin", "switch"}
)

DEFERRED_BRANCHES_PATH = "tasks/.deferred-branches.md"
RECHECK_DAYS = 7

_STALE_LINE = re.compile(r"^STALE-NEEDS-ABC branch=(?P<branch>\S+)", re.MULTILINE)
_RECONCILE_LINE = re.compile(
    r"^(?P<outcome>RECONCILE-CONFLICT-OPEN|RECONCILE-CONFLICT|RECONCILE-MERGE-NOT-STARTED"
    r"|RECONCILE-MERGE-COMMIT-REFUSED|RECONCILE-MACHINE-LOCAL-REFUSED)"
    r" branch=(?P<branch>\S+)\s*$"
)
_DEFERRABLE = frozenset({"RECONCILE-CONFLICT", "RECONCILE-MERGE-NOT-STARTED"})
HOLD_REASON = "reconcile-open"
HELD_STAGES: frozenset[str] = frozenset({"normalize", "triage", "memos"})
_EVIDENCE_KEY = re.compile(r"branch=(?P<branch>\S+) outcome=(?P<outcome>\S+)")
_PATH_MARKERS = ("CONFLICT-PATHS", "STAGED-PATHS")
STALE_RULE_POINT = "step0-stale-branch"
RECONCILE_RULE_POINT = "step0-reconcile-outcome"


def _decision(point: Mapping[str, Any], rule: str, value: str, rationale: str, evidence: str) -> Decision:
    return Decision(
        point_id=str(point.get("id", "")),
        question=str(point.get("question", "")),
        decision=value,
        decided_by="rule",
        rule=rule,
        rationale=rationale,
        evidence=evidence,
    )


def memo_inbox(point: Mapping[str, Any], *, context: Mapping[str, Any]) -> Decision | None:
    """Every memo point goes to the inbox agents; each memo's final disposition lands at finish."""
    point_id = str(point.get("id", ""))
    if not (point_id.startswith("j-memo-") or point_id == "j-overflow-memo"):
        return None
    inbox = context.get("inbox") or {}
    trigger = json.dumps(inbox.get("trigger") or {}, sort_keys=True)
    dispatches = len(inbox.get("dispatches") or [])
    evidence = f"{point.get('evidence', '')} | trigger={trigger} dispatches={dispatches}"
    return _decision(
        point,
        "memo-inbox",
        "routed-to-inbox-agents",
        "The inbox ends the run at zero; the trigger only sizes the dispatch.",
        evidence,
    )


def _run_git(args: list[str], cwd: Path) -> GitResult:
    return run_git(args, cwd=str(cwd))


def _write_deferral_line(path: Path, branch: str, run_id: str, today: _dt.date) -> None:
    recheck = today + _dt.timedelta(days=RECHECK_DAYS)
    line = (
        f"{branch} | stale-commit guard; run continued without reconciling"
        f" | re-check:{recheck.isoformat()} | {run_id}"
    )
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [ln for ln in existing if not ln.startswith(f"{branch} |")]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*kept, line]) + "\n", encoding="utf-8", newline="\n")


def step0_stale_branch(step: Any, result: Any, *, repo_root: Path, run_dir: Path) -> bool | str:
    """Step 0 exit 2: keep the branch, record one deferral line, commit that path alone."""
    match = _STALE_LINE.search(result.stdout or "")
    if match is None:
        return False
    branch = match["branch"]
    repo_root = Path(repo_root)
    run_id = Path(run_dir).name
    _write_deferral_line(repo_root / DEFERRED_BRANCHES_PATH, branch, run_id, _dt.date.today())
    sha = apply_base.scoped_commit(
        repo_root,
        DEFERRED_BRANCHES_PATH,
        f"defer stale branch {branch} (workday-start {run_id})",
        _run_git,
    )
    decide.record(
        Path(run_dir),
        [
            Decision(
                point_id=STALE_RULE_POINT,
                question=f"Branch {branch} is stale; reconcile it or keep it?",
                decision="keep-branch-without-reconciling",
                decided_by="rule",
                rule="step0-stale-branch",
                rationale="The run went on without reconciling the branch; the deferral line is the next reconcile's trigger.",
                evidence=f"branch={branch} file={DEFERRED_BRANCHES_PATH}",
                acted=(sha,) if sha else (),
            )
        ],
    )
    return True


def _paths_after_marker(stderr: str) -> list[str]:
    lines = (stderr or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip() in _PATH_MARKERS:
            return [p for p in (ln.strip() for ln in lines[index + 1 :]) if p]
    return []


def _previous_run_dir(run_dir: Path) -> Path | None:
    siblings = sorted(d for d in run_dir.parent.iterdir() if d.is_dir() and d.name < run_dir.name)
    return siblings[-1] if siblings else None


def _previously_decided(run_dir: Path, branch: str, outcome: str) -> bool:
    previous = _previous_run_dir(run_dir) if run_dir.parent.is_dir() else None
    if previous is None:
        return False
    try:
        rows = (previous / DECISIONS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for raw in rows:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("rule") != "step0-reconcile-outcome":
            continue
        found = _EVIDENCE_KEY.search(str(row.get("evidence", "")))
        if found and found["branch"] == branch and found["outcome"] == outcome:
            return True
    return False


def step0_reconcile_outcome(step: Any, result: Any, *, repo_root: Path, run_dir: Path) -> bool | str:
    """Step 0 exit 3: defer a conflict or a never-started merge once; the second sight fails the run.

    An open merge is neither: the rule records `resolve-in-run` and returns a `Hold`, so the
    committing stages wait while one resolver group edits the manifest's files and finish
    concludes the merge.

    Writes no tracked file, starts no child and commits nothing.
    """
    last = next((ln for ln in reversed((result.stdout or "").splitlines()) if ln.strip()), "")
    match = _RECONCILE_LINE.match(last.strip())
    if match is None or (match["outcome"] not in _DEFERRABLE and match["outcome"] != OUTCOME_CONFLICT_OPEN):
        return False
    branch, outcome = match["branch"], match["outcome"]
    run_dir = Path(run_dir)
    paths = _paths_after_marker(result.stderr or "")
    evidence = f"branch={branch} outcome={outcome} paths={','.join(paths)}"
    if outcome == OUTCOME_CONFLICT_OPEN:
        decide.record(
            run_dir,
            [
                Decision(
                    point_id=RECONCILE_RULE_POINT,
                    question=f"Reconcile of {branch} with origin/main left the merge open; what now?",
                    decision="resolve-in-run",
                    decided_by="rule",
                    rule="step0-reconcile-outcome",
                    rationale="A file-only resolver edits the listed paths; finish concludes the merge.",
                    evidence=evidence,
                )
            ],
        )
        return Hold(HOLD_REASON)
    repeated = _previously_decided(run_dir, branch, outcome)
    decide.record(
        run_dir,
        [
            Decision(
                point_id=RECONCILE_RULE_POINT,
                question=f"Reconcile of {branch} with origin/main ended {outcome}; what now?",
                decision="stop-and-file" if repeated else "deferred-once",
                decided_by="rule",
                rule="step0-reconcile-outcome",
                rationale=(
                    "The previous run already met this outcome on this branch; waiting does not clear it."
                    if repeated
                    else "First sight: the run continues on the unmerged branch."
                ),
                evidence=evidence,
            )
        ],
    )
    return f"reconcile-unresolved:{outcome}:{branch}" if repeated else True


NOMINATION_EXIT = 3
NO_NOMINATION_TEXT = "no nomination on record for "
NO_NOMINATION_OBSERVATION = "no nomination"


def nomination_none(step: Any, result: Any, *, repo_root: Path = None, run_dir: Path = None) -> bool:
    """`group-em-nomination who` exit 3 with the no-record text is an answer: the step ran.

    Any other exit, or exit 3 with other text, is not resolved. Writes and commits nothing.
    """
    if getattr(result, "exit_code", None) != NOMINATION_EXIT:
        return False
    text = f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"
    return NO_NOMINATION_TEXT in text


RULES: dict[str, Callable[..., Any]] = {
    "step0-stale-branch": step0_stale_branch,
    "step0-reconcile-outcome": step0_reconcile_outcome,
    RECOMMENDATION: recommendation,
    "memo-inbox": memo_inbox,
}

POINT_RULES = decide.RuleTable(
    point_rules=(("memo-inbox", memo_inbox), (RECOMMENDATION, recommendation)),
    exhausted_rule="no-live-disposition",
)
