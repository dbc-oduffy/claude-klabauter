"""coordinator_core.bash_guards._ownership_leg_stand_down -- when the commit
gate's ownership leg refused without a verdict.

Purpose: answer, in one place, whether an `assert_paths_in_session_scope`
denial rests on evidence at all -- and, when it does not, carry the durable
record that keeps the downgrade honest. Consumed by
`bash_guards/block_subagent_commit.py`'s
`_git_commit_agent_pathspec_permitted`, the single seam where that guard turns
a scope denial into a hard deny for `coordinator:git-commit-agent`.

THE ABSENCE THIS STANDS DOWN ON. The claim-index walk returned no verdict for
any denied path: `claim_index`'s `ABORT_CAUSE_EMPTY_BASE` (a cwd that resolves
to no repo at all -- the routine shape for a session with no launch anchor,
where `session.core.sessions_dir` answers `""` and every path comes back
`OWNERSHIP_UNANSWERABLE`), or `io_error`, or `cap_exceeded`.
`scope_report.denial_is_wholly_indeterminate` is that module's own predicate
for the state and its docstring names THIS guard as the consumer. It had none:
for the whole window between that predicate landing and this module, the
commit gate hard-denied on exactly the uncertainty the predicate was written
to stand down on. A guard that reads absence of a verdict as a negative
verdict denies on absence of evidence, which is the one thing every guard in
this family documents failing OPEN on.

Unconditional, and deliberately so -- unlike the write-confinement bumps'
`fleet_present` key. Those ask *is this somebody else's tree*, a question a
host can answer. This one asks *did anything answer at all*, and "no" is no
more informative on a workstation than on a container.

WHAT DOES NOT STAND DOWN, and each of these is the guard's real job:

  - A denial naming a HOLDER. `scope_report.deny_reason_names_a_holder` is
    consulted first and wins outright, so a live foreign-session claim still
    denies -- including in a pathspec where only one of several paths is held.
  - A DETERMINATE "nobody holds it" (`orphan`/`unclaimed`). This is the case
    that looks most like the one above and is not. The walk completed; the
    ledger names nobody. `claim_index`'s own NEGATIVE SPEC says what that is
    not -- absence from the index is NOT evidence that no session authored the
    path, because a file written by a shell redirect, a heredoc, or a spawned
    engine CLI is observed by no writer at all, which is how handoffs and
    frontmatter stamps routinely arrive. So the refusal IS resting on a
    reading the index itself disclaims, and it is why one wave of a workflow
    commits (its executor used `Edit`) and the next refuses (an engine op
    wrote the file) in the same run, minutes apart. It is nonetheless left
    DENYING here: reversing it widens a `CLASS = "hard-deny"` allow path
    against the strict-by-default posture DR-246 set deliberately
    (`include_orphans` an explicit opt-in, never a standing default) and
    SC-DR-022 narrowed further for dispatched agents. That is a ruling to
    change, not a predicate to patch, and the fix with no ruling in it is to
    make the engine's own writers register the claim -- an absent claim is an
    attribution defect in the writer, not an ownership fact about the path.
  - `include_orphans` from an agent. SC-DR-022 refuses the ASK, at
    `block_subagent_commit._LEG_AGENT_ORPHAN_ADOPTION`, before this module is
    reachable.
  - A pathspec SHAPE denial. `_pathspec_shape_permitted` runs first and
    returns its own `_LEG_*` sentinel; a sweeping element, an out-of-repo
    absolute, or an unresolvable repo root never reaches this module.
  - `already clean at HEAD`. A verdict about the tree, not about ownership.

Negative-spec:
  - Does NOT re-spell any classification literal. `scope_report` owns that
    vocabulary and a second copy of it in a guard is the drift shape
    `CLAIMED_BY_PREFIX`'s own comment records; the predicates are that
    module's, reached by import.
  - Does NOT decide the verdict for a path the ownership leg ALLOWED. It is
    consulted only after that leg has returned a denial.
  - Does NOT re-derive the overrides-log path or the audit-line format.
    `_write_bump_stand_down.log_environment_stand_down` owns both, and
    `_override_log_path` owns the never-mint-a-phantom-session rule beneath
    it; the marker below is what keeps this record distinguishable from the
    three write-bump streams sharing that sink.
  - Does NOT return a hook envelope, print a permission prompt, or offer an
    override key. It answers a bool and writes a line.
"""

from __future__ import annotations

from typing import Optional

STAND_DOWN_MARKER_NO_VERDICT = "STAND-DOWN-COMMIT-SCOPE-NO-VERDICT"

_STAND_DOWN_SINK_BASENAME = "commit-scope-stand-downs.log"

_EVIDENCE_NO_VERDICT = (
    "claim index returned no verdict for any denied path; absence of a "
    "verdict is not a negative verdict"
)


def _scope_report():
    try:
        from coordinator_core.ops.session import scope_report

        return scope_report
    except Exception:
        return None


def _denial_is_wholly_indeterminate(deny_reason: str) -> bool:
    scope_report = _scope_report()
    if scope_report is None:
        return False
    try:
        return bool(scope_report.denial_is_wholly_indeterminate(deny_reason))
    except Exception:
        return False


def _names_a_holder(deny_reason: str) -> bool:
    scope_report = _scope_report()
    if scope_report is None:
        return True
    try:
        return bool(scope_report.deny_reason_names_a_holder(deny_reason))
    except Exception:
        return True


def ownership_denial_stands_down(
    deny_reason: str,
    git_root: Optional[str],
    session_id: str,
) -> bool:
    if not isinstance(deny_reason, str) or not deny_reason:
        return False
    if _names_a_holder(deny_reason):
        return False
    if not _denial_is_wholly_indeterminate(deny_reason):
        return False
    _record(git_root, session_id, deny_reason)
    return True


def _record(git_root: Optional[str], session_id: str, deny_reason: str) -> None:
    try:
        from coordinator_core.bash_guards._write_bump_stand_down import (
            log_environment_stand_down,
            stand_down_notice,
        )
    except Exception:
        return
    summary = deny_reason.split(";", 1)[0]
    stand_down_notice(
        "block_subagent_commit: allowed a scoped commit the ownership leg "
        "refused without a verdict.\n%s\nRefusal stood down: %s"
        % (_EVIDENCE_NO_VERDICT, summary)
    )
    log_environment_stand_down(
        git_root,
        session_id,
        summary,
        _EVIDENCE_NO_VERDICT,
        marker=STAND_DOWN_MARKER_NO_VERDICT,
        sink_basename=_STAND_DOWN_SINK_BASENAME,
    )
