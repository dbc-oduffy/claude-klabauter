"""
coordinator_core.ops.ceremony.tests.test_sole_publisher_suppression

opro-01 C-01 (state/audits/2026-08-18-opro-01-where-the-push-outcome-is-known.md):
every commit through this op used to publish TWICE -- `git commit` fires the
`post-commit` hook, which detaches and pushes, and then `run_commit_pipeline`
runs its own synchronous `git push`. Two publishers racing for one branch tip is
what makes `integrity_breach` racy: when the detached child wins, this op's own
push fails on a commit that IS on the remote, and the op reports `PUSH FAILED --
commit is local only`. That is the 2026-07-30 false negative, and the reason
`scoped_git_commit` grew `_remote_sha_state` to interrogate the remote and walk
its own verdict back.

WHAT THESE TESTS PIN, and why each would catch a real regression rather than
restate the implementation:

  - The hook actually stands down on the env marker, doing NO git work at all
    (not "pushes but quietly") -- otherwise the second publisher is still there
    and the race is unchanged.
  - `os.environ` is never mutated. The engine is a cold spawn per invocation
    today, so a process-global toggle would pass every test here; under the warm
    engine it becomes a cross-request leak that suppresses a push for a caller
    that never asked. Pinned now, while it is cheap.
  - Suppression is wired to `push_mode == "sync"` and to NOTHING else. In
    `deferred`/`none` this op does not push, so the hook's push is the only one
    there is -- suppressing it there strands the commit unpushed, which is a
    worse failure than the one C-01 fixes.
  - The `drain_pending_push` call site that used to ride on every commit's own
    detached push is re-hosted, not dropped.

Spec backlink: state/handoffs/2026-08-18_190000_roadmap-opro-01.md (C-01)
               state/audits/2026-08-18-opro-01-where-the-push-outcome-is-known.md
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.hooks import auto_push
from coordinator_core.ops.ceremony import git_native

_ENV = "COORDINATOR_AUTO_PUSH_SUPPRESS_FOR_SYNC_PUSH"


# ---------------------------------------------------------------------------
# The hook's own stand-down
# ---------------------------------------------------------------------------


def test_auto_push_stands_down_entirely_under_the_marker(monkeypatch):
    """`main()` returns 0 having issued no git call and taken no detach.

    Asserted against the git seam and the detach seam rather than the return
    value: `main()` returns 0 unconditionally by its own never-block-a-commit
    contract, so a return code proves nothing here.
    """
    calls: list = []
    monkeypatch.setattr(auto_push, "_run_git", lambda *a, **kw: calls.append(a) or None)
    monkeypatch.setattr(
        auto_push, "_detach_and_run", lambda *a, **kw: calls.append(("detach", a))
    )
    monkeypatch.setattr(
        auto_push, "run_push_with_retry", lambda *a, **kw: calls.append(("push", a))
    )
    monkeypatch.setenv(_ENV, "1")

    assert auto_push.main([]) == 0
    assert calls == [], "suppressed hook still did work: %r" % (calls,)


def test_auto_push_runs_normally_without_the_marker(monkeypatch):
    """The complement, so the test above cannot pass by breaking the hook.

    Without this pair, a `main()` that stood down unconditionally -- every
    commit silently unpushed -- would look exactly like a passing C-01.
    """
    monkeypatch.delenv(_ENV, raising=False)
    reached: list = []
    monkeypatch.setattr(auto_push, "_run_git", lambda *a, **kw: "/tmp/repo")
    monkeypatch.setattr(auto_push, "resolve_branch", lambda root: "work/x/y")
    monkeypatch.setattr(auto_push, "branch_gate", lambda b: (True, None))
    monkeypatch.setattr(
        auto_push, "_detach_and_run", lambda *a, **kw: reached.append(a)
    )

    assert auto_push.main([]) == 0
    assert reached, "unsuppressed hook did not reach its push path"


# ---------------------------------------------------------------------------
# The env the commit spawn carries
# ---------------------------------------------------------------------------


def test_sole_publisher_env_is_none_when_off():
    """None, not a rebuilt `dict(os.environ)`.

    `_git` treats None as "inherit the parent environment unchanged" and a dict
    as "replace it wholesale". Returning a copy would be a behaviour change
    wearing a no-op's clothes -- every commit would suddenly run under a
    snapshot taken at call time.
    """
    assert git_native._sole_publisher_env(False) is None


def test_sole_publisher_env_sets_the_marker_without_touching_os_environ():
    before = dict(os.environ)
    env = git_native._sole_publisher_env(True)

    assert env is not None and env[_ENV] == "1"
    assert _ENV not in os.environ, (
        "suppression leaked into os.environ -- a cold-spawn engine hides this, "
        "a warm one turns it into a cross-request leak"
    )
    assert dict(os.environ) == before


def test_marker_name_matches_the_hook_that_reads_it():
    """The two modules name the same string.

    They are deliberately not a shared import (the hook must stay importable
    with no dependency on the ceremony package), so nothing but this assertion
    stops a rename on one side from silently disarming the other -- the commit
    path would go on setting a variable no reader consults, and the race would
    return with every test still green.
    """
    assert git_native._AUTO_PUSH_SUPPRESS_ENV == auto_push._ENV_SUPPRESS_FOR_SYNC_PUSH


# ---------------------------------------------------------------------------
# Wiring: sync and nothing else
# ---------------------------------------------------------------------------


# `test_suppression_is_wired_to_sync_mode_only`,
# `test_commit_forwards_suppression_to_commit_scoped`,
# `test_pending_push_drain_is_rehosted_not_dropped`, and
# `test_drain_failure_never_fails_a_landed_push` (all deleted, C4 of
# docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-the-pipeline-can-
# go.md): they pinned `commit_pipeline.commit()`'s OWN wiring of
# `suppress_post_commit_auto_push` and `commit_pipeline._drain_pending_push_
# after_sync`, both of which died with the module. The suppression
# MECHANISM itself (`git_native._sole_publisher_env`,
# `deferred_publisher_span()`) is unowned by `commit_pipeline` and is pinned
# below, unchanged; the wiring of a SURVIVING caller into that mechanism
# (`push_mode == PUSH_MODE_SYNC` -> `suppress_post_commit_auto_push=True`)
# is that caller's own test file's job now -- see
# `ops/ceremony/tests/test_post_commit_tail.py` and
# `ops/ceremony/tests/test_consumed_handoff_stamp.py`, both of which already
# drive `push_mode` through their own real call sites onto
# `git_native.commit_scoped` directly.


# ---------------------------------------------------------------------------
# C5 (docs/plans/2026-08-19-windows-commit-hook-starts-python-once.md):
# the deferred-path backstop -- one publisher on the DEFAULT (deferred) path.
# ---------------------------------------------------------------------------


def test_deferred_publisher_span_widens_effective_suppression():
    """`deferred_publisher_span()` makes `_sole_publisher_env(False)` suppress.

    This is the EFFECTIVE-layer counterpart `test_suppression_is_wired_to_
    sync_mode_only`'s own docstring points to: the per-call argument stays
    False (unrelated to `push_mode`), yet the env now carries the marker,
    because a span is active. Outside the span, `False` still means `None`
    -- the widening is additive, never a replacement for the original wire.
    """
    assert git_native._sole_publisher_env(False) is None
    with git_native.deferred_publisher_span():
        env = git_native._sole_publisher_env(False)
        assert env is not None and env[_ENV] == "1"
    assert git_native._sole_publisher_env(False) is None, (
        "suppression outlived the span -- the contextvar token was not reset"
    )


def test_deferred_publisher_span_never_touches_os_environ():
    before = dict(os.environ)
    with git_native.deferred_publisher_span():
        git_native._sole_publisher_env(False)
        assert dict(os.environ) == before
    assert dict(os.environ) == before
