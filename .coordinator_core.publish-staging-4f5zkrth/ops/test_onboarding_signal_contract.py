"""
coordinator_core.ops.test_onboarding_signal_contract — regression guard for
C1 of docs/plans/2026-08-14-retire-the-handoff-tracker-and-project-tracker-
renders.md: `detect_onboarding_offer._is_onboarded` and
`workstream_complete.directives_completion.completion_archive_predicate`
both stopped reading `docs/project-tracker.md` presence as their onboarding
signal (the file is retired fleet-wide by a later chunk in that plan) and
now both check `archive/` OR `state/workstreams/`, so the two predicates
are genuinely identical and cannot drift apart.

Review note (coordinator:code-reviewer P1/P2, 2026-08-14): the original
chunk repointed both gates onto `state/workstreams/` alone. That signal is
empirically unsound -- it's created lazily by queue_append.py on first
workstream event, no install/scaffold path provisions it, and 11 of 12
currently-onboarded sibling repos in the fleet have no `state/workstreams/`
dir. `archive/` was already the pre-existing arm of
`completion_archive_predicate` and is present on all of those repos, so it
was added to `_is_onboarded` too -- fixing the false-negative and closing
the drift gap the P2 finding identified (an archive/-only repo used to
report ONBOARDED from one gate and UNONBOARDED from the other).

Negative-spec: a repo with NO `docs/project-tracker.md` anywhere on disk
must still report onboarded once `archive/` or `state/workstreams/` exists
-- the failure mode this test exists to catch is a correct-looking
"unonboarded" offer (or a Step 2.6 skip) appearing everywhere once the file
is deleted fleet-wide.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.detect_onboarding_offer import _is_onboarded
from coordinator_core.workstream_complete.directives_completion import (
    completion_archive_predicate,
)


def test_onboarded_without_project_tracker_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "state" / "workstreams").mkdir(parents=True)

    assert not (repo / "docs" / "project-tracker.md").exists()
    assert _is_onboarded(str(repo)) is True


def test_unonboarded_without_workstreams_or_tracker(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert _is_onboarded(str(repo)) is False


def test_step_2_6_gate_unchanged_for_onboarded_repo_via_archive_arm(tmp_path: Path) -> None:
    """Step 2.6's `archive/` arm is untouched by this chunk -- an onboarded
    repo that only has `archive/` (no `state/workstreams/`, no tracker
    file) still trips the gate exactly as before. Also calls `_is_onboarded`
    on the same repo (Review: coordinator:code-reviewer P2) -- the original
    version of this test asserted only the Step 2.6 arm, which is exactly
    why it didn't catch the two gates disagreeing on this shape."""
    repo = tmp_path / "repo"
    (repo / "archive").mkdir(parents=True)

    assert completion_archive_predicate(repo) is True
    assert _is_onboarded(str(repo)) is True


def test_step_2_6_gate_true_via_workstreams_without_tracker_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "state" / "workstreams").mkdir(parents=True)

    assert not (repo / "docs" / "project-tracker.md").exists()
    assert completion_archive_predicate(repo) is True


def test_step_2_6_gate_false_when_neither_signal_present(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert completion_archive_predicate(repo) is False


def test_signals_agree_across_both_gates(tmp_path: Path) -> None:
    """The two gates must not drift: same repo, same disk state, same
    onboarded verdict from both."""
    onboarded_repo = tmp_path / "onboarded"
    (onboarded_repo / "state" / "workstreams").mkdir(parents=True)
    assert _is_onboarded(str(onboarded_repo)) == completion_archive_predicate(onboarded_repo)

    unonboarded_repo = tmp_path / "unonboarded"
    unonboarded_repo.mkdir()
    assert _is_onboarded(str(unonboarded_repo)) == completion_archive_predicate(unonboarded_repo)


def test_signals_agree_on_archive_only_repo(tmp_path: Path) -> None:
    """Review: coordinator:code-reviewer (P2 drift finding) -- this is the
    case that actually disagreed before `archive/` was added to
    `_is_onboarded`: an archive/-only repo (no state/workstreams/) used to
    report `_is_onboarded() is False` while `completion_archive_predicate()
    is True`, falsifying the "cannot drift apart" docstring claim. This
    case would have caught the regression; the prior both-present/
    both-absent pair above would not."""
    repo = tmp_path / "archive-only"
    (repo / "archive").mkdir(parents=True)

    assert _is_onboarded(str(repo)) == completion_archive_predicate(repo) is True


def test_onboarded_shaped_like_real_sibling_repo(tmp_path: Path) -> None:
    """Review: coordinator:code-reviewer (P1) -- shape a fixture the way the
    real fleet actually looks: no `docs/project-tracker.md`, no
    `state/workstreams/`, only `archive/` (11 of 12 currently-onboarded
    sibling repos surveyed under the fleet had exactly this shape). Both
    gates must report ONBOARDED for it."""
    repo = tmp_path / "sibling-shaped"
    (repo / "archive").mkdir(parents=True)
    (repo / "state" / "handoffs").mkdir(parents=True)

    assert not (repo / "docs" / "project-tracker.md").exists()
    assert not (repo / "state" / "workstreams").exists()
    assert _is_onboarded(str(repo)) is True
    assert completion_archive_predicate(repo) is True
