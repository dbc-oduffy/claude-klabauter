"""
coordinator_core.ops.ceremony.tests.test_push

Pins the ref-lock deferral status and its two surrounding negative specs
(C2, docs/dispatch-briefs/2026-08-30-push-outstanding-stops-declining-ref-
lock/C2.md): a ref-lock rejection resolves to `derive_push_status(...) ==
"cadence-pending"`, `git_native.push` is called exactly ONCE for it (the PM
ruling's guard -- no fetch, no rebase, no retry), and the three sibling
outcomes a careless edit to the same retry ladder could sweep into that new
marker by mistake -- a genuine `non-fast-forward` (still `push-failed` after
its existing poll ladder, same attempt count as before), a `gh-push-
protection` rejection (still `push-failed`, zero retries, never swept into
the ref-lock marker just because it shares the loop), and a subprocess
timeout (still `unconfirmed`, never collapsed into `cadence-pending` --
"we do not know it landed" is not the same claim as "it will be retried at
the next checkpoint") -- stay exactly where they were.

File name deliberately `test_push.py`: `dispatch_emit` maps a written path
to `tests/test_<stem>.py` at each ancestor, and `push.py` had no stem-named
test on any rung before this file (its existing tests are all topic-named --
`test_push_reject_landed_by_peer.py`, `test_push_rule_violation_class.py`,
`test_push_import_surface.py`, `test_commit_pipeline_push_spawns.py`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native, push as push_mod
from coordinator_core.ops.ceremony.git_native import GitResult

pytestmark = [pytest.mark.spawns_process]


_REF_LOCK_STDERR = (
    "! [remote rejected] work/x -> work/x (failed to lock references)\n"
    "error: failed to push some refs to 'origin'\n"
)

_NON_FAST_FORWARD_STDERR = (
    "! [rejected] work/x -> work/x (non-fast-forward)\n"
    "error: failed to push some refs to 'origin'\n"
    "hint: Updates were rejected because the tip of your current branch is behind\n"
)

_GH013_SECRET_SCANNING_STDERR = (
    "remote: error: GH013: Repository rule violations found for refs/heads/work/x.\n"
    "remote: - Push cannot contain secrets\n"
    "remote: — Secret detected: Generic Credential\n"
    "! [remote rejected] work/x -> work/x (push declined due to repository rule violations)\n"
    "error: failed to push some refs to 'origin'\n"
)

_TIMEOUT_STDERR = "git push origin work/x: timed out after 5s (killed)"


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(tmp_path: Path, *, with_upstream: bool) -> Path:
    """A `work/x` repo with a locally-declared `origin` remote -- enough for
    `_remote_configured_locally`/`branch_gate` to pass, since `push` itself
    is mocked in every test below and never talks to it.

    `with_upstream=True` additionally pushes to a real bare origin so
    `branch.<name>.remote`/`.merge` genuinely exist -- required only by the
    non-fast-forward regression test, which must reach the fetch/rebase
    ladder (`_resolve_upstream_local` reads `.git/config` for those keys).
    Every other outcome here breaks out of the retry loop on the FIRST
    reject, before `push_with_retry` ever resolves an upstream, so it does
    not need one.
    """
    if with_upstream:
        origin = tmp_path / "origin.git"
        _git(["init", "-q", "--bare", str(origin)], tmp_path)
        origin_url = str(origin)
    else:
        origin_url = str(tmp_path / "origin-unused.git")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", origin_url], repo)
    if with_upstream:
        _git(["push", "-q", "-u", "origin", "work/x"], repo)
    return repo


def _always_reject(monkeypatch, stderr: str, push_calls: list, *, returncode: int = 1) -> None:
    def _fake_push(*a, **kw):
        push_calls.append(1)
        return GitResult(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(git_native, "push", _fake_push)


def test_ref_lock_rejection_is_cadence_pending_single_attempt(tmp_path, monkeypatch):
    """Assertion 1+2: a ref-lock rejection deferred, not failed -- exactly
    ONE `git_native.push` call, no retry/fetch/rebase (the PM ruling's
    guard)."""
    repo = _init_repo(tmp_path, with_upstream=False)
    push_calls: list = []
    _always_reject(monkeypatch, _REF_LOCK_STDERR, push_calls)

    fetch_calls: list = []
    monkeypatch.setattr(
        git_native,
        "fetch",
        lambda *a, **kw: fetch_calls.append(1) or GitResult(returncode=0, stdout="", stderr=""),
    )
    rebase_calls: list = []
    monkeypatch.setattr(
        push_mod,
        "_rebase_onto_fetched_ref",
        lambda *a, **kw: rebase_calls.append(1) or (1, "should never run"),
    )

    outcome = push_mod.push_with_retry(repo)

    assert len(push_calls) == 1
    assert fetch_calls == []
    assert rebase_calls == []
    assert outcome.exit_code == 0
    assert outcome.failed == []
    assert outcome.unconfirmed == []
    assert outcome.skipped == ["push:ref-contended"]
    assert push_mod.derive_push_status(outcome) == push_mod.PUSH_STATUS_CADENCE_PENDING


def test_genuine_non_fast_forward_still_reaches_push_failed_same_attempt_count(
    tmp_path, monkeypatch
):
    """Assertion 3: a real non-fast-forward reject still exhausts the
    existing poll ladder and lands `push-failed`, with the same attempt
    count as before this chunk touched the shared loop."""
    repo = _init_repo(tmp_path, with_upstream=True)
    push_calls: list = []
    _always_reject(monkeypatch, _NON_FAST_FORWARD_STDERR, push_calls)
    monkeypatch.setattr(
        git_native, "fetch", lambda *a, **kw: GitResult(returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(
        push_mod,
        "_rebase_onto_fetched_ref",
        lambda *a, **kw: (1, "git rebase: conflict"),
    )
    monkeypatch.setattr(
        push_mod,
        "_head_already_reached_upstream",
        lambda *a, **kw: False,
    )

    outcome = push_mod.push_with_retry(repo)

    assert len(push_calls) == 1
    assert outcome.exit_code != 0
    assert outcome.unconfirmed == []
    assert outcome.failed
    assert push_mod.derive_push_status(outcome) == push_mod.PUSH_STATUS_FAILED


def test_gh_push_protection_still_push_failed_zero_retries(tmp_path, monkeypatch):
    """Assertion 4: a `gh-push-protection` rejection (secret-scanning
    sub-class) shares the reject-detect loop but must not be swept into the
    new ref-lock marker -- still `push-failed`, zero retries."""
    repo = _init_repo(tmp_path, with_upstream=False)
    push_calls: list = []
    _always_reject(monkeypatch, _GH013_SECRET_SCANNING_STDERR, push_calls)

    fetch_calls: list = []
    monkeypatch.setattr(
        git_native,
        "fetch",
        lambda *a, **kw: fetch_calls.append(1) or GitResult(returncode=0, stdout="", stderr=""),
    )

    outcome = push_mod.push_with_retry(repo)

    assert len(push_calls) == 1
    assert fetch_calls == []
    assert outcome.exit_code != 0
    assert outcome.unconfirmed == []
    assert outcome.failed
    assert outcome.skipped == []
    assert push_mod.derive_push_status(outcome) == push_mod.PUSH_STATUS_FAILED


def test_timeout_is_unconfirmed_never_cadence_pending(tmp_path, monkeypatch):
    """Assertion 5: a push subprocess timeout resolves to `unconfirmed`,
    never `cadence-pending` -- both mean "we do not know it landed", but
    `derive_push_status`'s own docstring calls `unconfirmed` the worst of
    the three states, and collapsing them would lose that distinction."""
    repo = _init_repo(tmp_path, with_upstream=False)
    push_calls: list = []
    _always_reject(monkeypatch, _TIMEOUT_STDERR, push_calls, returncode=-1)

    outcome = push_mod.push_with_retry(repo)

    assert len(push_calls) == 1
    assert outcome.failed == []
    assert outcome.skipped == []
    assert outcome.unconfirmed
    assert push_mod.derive_push_status(outcome) == push_mod.PUSH_STATUS_UNCONFIRMED
