"""
coordinator_core.ops.ceremony.tests.test_push

Pins the ref-lock deferral status and its one surrounding negative spec
(C2, docs/dispatch-briefs/2026-08-30-push-outstanding-stops-declining-ref-
lock/C2.md): a ref-lock rejection resolves to `derive_push_status(...) ==
"cadence-pending"`, `git_native.push` is called exactly ONCE for it (the PM
ruling's guard -- no fetch, no rebase, no retry), and a genuine
`non-fast-forward` reject (still `push-failed` after its existing poll
ladder, same attempt count as before) stays exactly where it was.

Assertions 4-5 of the C2 spec -- a `gh-push-protection` rejection staying
`push-failed` with zero retries, and a subprocess timeout staying
`unconfirmed` never `cadence-pending` -- are discharged by named siblings
rather than duplicated here (Review: overengineering-reviewer -- both were
near-clones of `test_push_rule_violation_class.py::
test_secret_scanning_reject_is_never_repushed_even_though_it_shares_gh_push_protection`
and `::test_push_subprocess_timeout_still_yields_unconfirmed_not_failed_in_c2_neighbourhood`,
verified to share the same stderr fixture and assert the same outcomes;
the `classify_error` discrimination they rest on is separately pinned in
`coordinator_core/hooks/test_auto_push.py`).

File name deliberately `test_push.py`: stem-named per `dispatch_emit`'s
`tests/test_<stem>.py` mapping, which `push.py` had no file for.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony import git_native, push as push_mod
from coordinator_core.ops.ceremony.tests.fixtures.push_repo import init_push_repo
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


def _always_reject(monkeypatch, stderr: str, push_calls: list, *, returncode: int = 1) -> None:
    def _fake_push(*a, **kw):
        push_calls.append(1)
        return GitResult(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(git_native, "push", _fake_push)
    # `init_push_repo`'s default fixture carries a configured (same-name)
    # upstream, so `push_with_retry` now pushes by explicit refspec (see
    # `push.push_with_retry`'s upstream_info branch) -- both call shapes
    # must reject identically for these attempt-count assertions to hold.
    monkeypatch.setattr(git_native, "push_refspec", _fake_push)


def test_ref_lock_rejection_is_cadence_pending_single_attempt(tmp_path, monkeypatch):
    """Assertion 1+2: a ref-lock rejection deferred, not failed -- exactly
    ONE `git_native.push` call, no retry/fetch/rebase (the PM ruling's
    guard)."""
    repo = init_push_repo(tmp_path)
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
    repo = init_push_repo(tmp_path)
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


# ---------------------------------------------------------------------------
# P052-C6 (docs/plans/2026-09-10-push-cadence-hang-detection-over-elapsed-
# timeout.md): `_is_indeterminate_push_result`'s two-arm truth table, and a
# stall-kill routed to `PushOutcome.unconfirmed` through `push_with_retry`.
# ---------------------------------------------------------------------------


def test_is_indeterminate_push_result_stall_arm_with_real_returncode():
    """`stall_killed=True` with a real signal returncode (never the -1 the
    elapsed-timeout arm reads) gives True."""
    result = GitResult(
        returncode=-15, stdout="", stderr="Writing objects: 10%\n", stall_killed=True
    )
    assert push_mod._is_indeterminate_push_result(result) is True


def test_is_indeterminate_push_result_existing_timeout_arm_unchanged():
    """The pre-existing arm's text is untouched: `returncode == -1` plus a
    `"timed out after N"` stderr blob still gives True."""
    result = GitResult(returncode=-1, stdout="", stderr="fatal: timed out after 30s")
    assert push_mod._is_indeterminate_push_result(result) is True


def test_is_indeterminate_push_result_oserror_returncode_minus_one_stays_false():
    """`returncode == -1` from an `OSError`/git-not-on-PATH result --
    `stall_killed=False`, no `"timed out after N"` text -- must stay False.
    The negative spec this arm must never collapse into."""
    result = GitResult(returncode=-1, stdout="", stderr="OSError: [Errno 2] No such file or directory: 'git'")
    assert result.stall_killed is False
    assert push_mod._is_indeterminate_push_result(result) is False


def test_marker_line_alone_does_not_match_the_timeout_regex():
    """`git_native.PUSH_STALL_MARKER`, formatted, must not match
    `_PUSH_TIMEOUT_RE` on its own -- the two consumers (the typed field and
    the marker line) are deliberately independent channels."""
    marker_text = git_native.PUSH_STALL_MARKER.format(secs=14.036)
    assert push_mod._PUSH_TIMEOUT_RE.search(marker_text) is None
    # And a GitResult carrying the marker line but no stall_killed flag set
    # is NOT indeterminate on the marker text alone -- only the typed field
    # (or the existing timeout-text arm) can make this predicate True.
    result = GitResult(returncode=0, stdout="", stderr=marker_text)
    assert push_mod._is_indeterminate_push_result(result) is False


def test_stall_kill_routes_to_unconfirmed_never_failed_never_retried(tmp_path, monkeypatch):
    """A stall-kill on the streamed push leg must land in
    `PushOutcome.unconfirmed`, never `failed`, and the ladder must not
    retry it blind (one `push_streamed` call only)."""
    repo = init_push_repo(tmp_path, set_upstream=False)
    stall_calls: list = []

    def _fake_push_streamed(*a, **kw):
        stall_calls.append(1)
        return GitResult(
            returncode=-15,
            stdout="",
            stderr="Writing objects: 10%\n" + git_native.PUSH_STALL_MARKER.format(secs=0.05),
            stall_killed=True,
        )

    monkeypatch.setattr(git_native, "push_streamed", _fake_push_streamed)

    outcome = push_mod.push_with_retry(repo, use_streamed_push=True)

    assert len(stall_calls) == 1
    assert outcome.failed == []
    assert outcome.unconfirmed
    assert push_mod.derive_push_status(outcome) == push_mod.PUSH_STATUS_UNCONFIRMED


def test_budget_exhaustion_still_yields_failed_not_unconfirmed(tmp_path, monkeypatch):
    """NEGATIVE SPEC pinned again alongside the stall-kill test above: an
    exhausted `budget_secs` ladder deadline is `failed`, never
    `unconfirmed` -- a stall-kill must not widen this existing distinction."""
    repo = init_push_repo(tmp_path)
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

    outcome = push_mod.push_with_retry(repo, budget_secs=0.0)

    assert outcome.unconfirmed == []
    assert outcome.failed
    assert push_mod.derive_push_status(outcome) == push_mod.PUSH_STATUS_FAILED
