"""
coordinator_core.ops.ceremony.tests.test_push_no_upstream_publish

Pins the no-upstream publish arm added to `push.publish_day_branch` and
`push.push_with_retry` on 2026-09-02.

The defect: `work/machine-b/2026-09-02` was cut by the SessionStart boot
path, which by contract makes no network call, and NOTHING else ever gave it
an upstream -- the `auto_push.push_once` the boot path's comment named had
had no per-commit caller since C6/C7 of
`docs/plans/2026-08-30-who-pushes-and-when.md`. The cadence sweep then swept
the branch every 600s, took `fatal: The current branch ... has no upstream
branch` from a bare `git push` every time, logged it to
`.git/push-failures.log`, and gave up -- while 102 commits of the day's work
sat with no remote copy.

Three assertions, one per hazard the fix has to hold apart:
  1. the no-upstream refusal publishes and lands (the repair),
  2. every OTHER push failure is untouched by it (the discrimination),
  3. a branch the day-branch oracle declines is never published (blast
     radius).
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import git_native, push as push_mod
from coordinator_core.ops.ceremony.git_native import GitResult
from coordinator_core.ops.ceremony.tests.fixtures.push_repo import init_push_repo

pytestmark = [pytest.mark.spawns_process]


#: git's own words, verbatim from `.git/push-failures.log`'s 2026-09-02 row.
_NO_UPSTREAM_STDERR = (
    "fatal: The current branch work/machine-b/2026-09-02 has no upstream branch.\n"
    "To push the current branch and set the remote as upstream, use\n"
    "\n"
    "    git push --set-upstream origin work/machine-b/2026-09-02\n"
)

_AUTH_STDERR = (
    "ERROR: Permission to dbc-oduffy/claude-klabauter.git denied to nobody.\n"
    "fatal: Could not read from remote repository.\n"
)

_DAY_BRANCH = "work/machine-b/2026-09-02"
#: Satisfies `auto_push.branch_gate` (it is `work/*`) but NOT
#: `daily_branch.is_canonical_branch` -- the collision-suffix shape
#: `session_ensure_branch`'s ceremony arm mints. The negative control has to
#: be a name the pre-existing gate would wave through, or it proves nothing
#: about the new one.
_NON_DAY_BRANCH = "work/machine-b/2026-09-02-2"


def _bare_push_always_refuses(monkeypatch, stderr: str, calls: list) -> None:
    """Make the refspec-less `git push` fail; leave `push_set_upstream` real.

    Only the BARE form is mocked, deliberately: the publish that follows must
    reach a genuine `git push --set-upstream` against the fixture's real bare
    origin, so "it published" means a ref actually moved on a remote, not that
    a mock returned zero.
    """

    def _fake_push(*a, **kw):
        calls.append(1)
        return GitResult(returncode=128, stdout="", stderr=stderr)

    monkeypatch.setattr(git_native, "push", _fake_push)


def _remote_has(repo, branch: str) -> bool:
    origin = repo.parent / "origin.git"
    proc = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(origin),
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode == 0


def test_no_upstream_refusal_publishes_the_day_branch_and_reports_a_landed_push(
    tmp_path, monkeypatch
):
    """The repair. A day branch with no upstream takes git's no-upstream
    refusal ONCE, publishes with `--set-upstream`, and reports a landed push
    -- and the branch is genuinely on the remote with tracking configured
    afterwards, so every LATER bare push on it works without this arm.
    """
    repo = init_push_repo(tmp_path, branch=_DAY_BRANCH, set_upstream=False)
    assert not _remote_has(repo, _DAY_BRANCH)

    push_calls: list = []
    _bare_push_always_refuses(monkeypatch, _NO_UPSTREAM_STDERR, push_calls)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    assert not outcome.failed
    assert not outcome.unconfirmed
    # Exactly one bare attempt: the publish replaces the ladder, never
    # re-enters it.
    assert len(push_calls) == 1
    assert _remote_has(repo, _DAY_BRANCH)
    assert push_mod._resolve_upstream_local(repo, _DAY_BRANCH) is not None


def test_an_unrelated_push_failure_never_publishes(tmp_path, monkeypatch):
    """The discrimination. The SAME repo state -- day branch, no upstream --
    but git refuses for a reason `--set-upstream` does not address. The arm
    must not fire: a publish here would be an unrequested write to the remote
    triggered by an unrelated fault.
    """
    repo = init_push_repo(tmp_path, branch=_DAY_BRANCH, set_upstream=False)
    push_calls: list = []
    _bare_push_always_refuses(monkeypatch, _AUTH_STDERR, push_calls)

    set_upstream_calls: list = []
    monkeypatch.setattr(
        git_native,
        "push_set_upstream",
        lambda *a, **kw: set_upstream_calls.append(1)
        or GitResult(returncode=0, stdout="", stderr=""),
    )

    outcome = push_mod.push_with_retry(repo)

    assert set_upstream_calls == []
    assert outcome.failed
    assert outcome.exit_code != 0
    assert not _remote_has(repo, _DAY_BRANCH)


def test_a_non_day_branch_is_never_published_by_this_path(tmp_path, monkeypatch):
    """The blast radius. `work/machine-b/2026-09-02-2` passes
    `auto_push.branch_gate` (it is `work/*`) and takes the identical
    no-upstream refusal, but `daily_branch.is_canonical_branch` declines it,
    so nothing is sent to the remote.

    Negative spec: the publish authorisation is for the DAY BRANCH. If this
    test starts failing because the gate was widened to a name pattern, the
    widening is the defect, not the test.
    """
    repo = init_push_repo(tmp_path, branch=_NON_DAY_BRANCH, set_upstream=False)
    push_calls: list = []
    _bare_push_always_refuses(monkeypatch, _NO_UPSTREAM_STDERR, push_calls)

    set_upstream_calls: list = []
    monkeypatch.setattr(
        git_native,
        "push_set_upstream",
        lambda *a, **kw: set_upstream_calls.append(1)
        or GitResult(returncode=0, stdout="", stderr=""),
    )

    outcome = push_mod.push_with_retry(repo)

    assert set_upstream_calls == []
    assert not _remote_has(repo, _NON_DAY_BRANCH)
    assert outcome.failed
    assert "not a canonical day branch" in outcome.failed[0]


def test_publish_day_branch_declines_every_shape_it_must(tmp_path):
    """`publish_day_branch`'s own gates, exercised directly -- the four
    decline arms plus the idempotent already-published one, none of which
    reaches the network.
    """
    repo = init_push_repo(tmp_path, branch=_DAY_BRANCH, set_upstream=True)

    assert push_mod.publish_day_branch(repo, branch=_DAY_BRANCH)[0] == "already-published"
    assert push_mod.publish_day_branch(repo, branch="main")[0] == "declined-not-day-branch"
    assert (
        push_mod.publish_day_branch(repo, branch=_NON_DAY_BRANCH)[0]
        == "declined-not-day-branch"
    )
    # Mixed case is `is_allowed_branch` but NOT `is_canonical_branch` -- the
    # creation oracle, chosen here on purpose (Windows case-insensitive-FS
    # ref hazard).
    assert (
        push_mod.publish_day_branch(repo, branch="work/Machine-b/2026-09-02")[0]
        == "declined-not-day-branch"
    )
    assert push_mod.publish_day_branch(repo, branch="")[0] == "declined-unresolvable"
