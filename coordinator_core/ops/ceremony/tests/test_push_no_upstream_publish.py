
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import git_native, push as push_mod
from coordinator_core.ops.ceremony.git_native import GitResult
from coordinator_core.ops.ceremony.tests.fixtures.push_repo import init_push_repo

pytestmark = [pytest.mark.spawns_process]


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
_NON_DAY_BRANCH = "work/machine-b/2026-09-02-2"


def _bare_push_always_refuses(monkeypatch, stderr: str, calls: list) -> None:

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
    repo = init_push_repo(tmp_path, branch=_DAY_BRANCH, set_upstream=False)
    assert not _remote_has(repo, _DAY_BRANCH)

    push_calls: list = []
    _bare_push_always_refuses(monkeypatch, _NO_UPSTREAM_STDERR, push_calls)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    assert not outcome.failed
    assert not outcome.unconfirmed
    assert len(push_calls) == 1
    assert _remote_has(repo, _DAY_BRANCH)
    assert push_mod._resolve_upstream_local(repo, _DAY_BRANCH) is not None


def test_an_unrelated_push_failure_never_publishes(tmp_path, monkeypatch):
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
    repo = init_push_repo(tmp_path, branch=_DAY_BRANCH, set_upstream=True)

    assert push_mod.publish_day_branch(repo, branch=_DAY_BRANCH)[0] == "already-published"
    assert push_mod.publish_day_branch(repo, branch="main")[0] == "declined-not-day-branch"
    assert (
        push_mod.publish_day_branch(repo, branch=_NON_DAY_BRANCH)[0]
        == "declined-not-day-branch"
    )
    assert (
        push_mod.publish_day_branch(repo, branch="work/Machine-b/2026-09-02")[0]
        == "declined-not-day-branch"
    )
    assert push_mod.publish_day_branch(repo, branch="")[0] == "declined-unresolvable"
