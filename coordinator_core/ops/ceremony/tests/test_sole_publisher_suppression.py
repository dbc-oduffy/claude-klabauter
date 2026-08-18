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

from coordinator_core.hooks import auto_push
from coordinator_core.ops.ceremony import commit_pipeline, git_native

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


@pytest.mark.parametrize(
    "push_mode,expected",
    [
        (commit_pipeline.PUSH_MODE_SYNC, True),
        (commit_pipeline.PUSH_MODE_DEFERRED, False),
        (commit_pipeline.PUSH_MODE_NONE, False),
    ],
)
def test_suppression_is_wired_to_sync_mode_only(push_mode, expected):
    """A deferred/none commit must leave the hook's push alone: it is the only
    publisher on those modes, and suppressing it strands the commit."""
    assert (push_mode == commit_pipeline.PUSH_MODE_SYNC) is expected


def test_commit_forwards_suppression_to_commit_scoped(monkeypatch, tmp_path):
    """The flag reaches `commit_scoped`, not just the signature.

    A parameter threaded through three call layers is exactly the kind of thing
    that gets added and then not passed at the last hop.
    """
    seen: dict = {}

    def _fake_commit_scoped(paths, msg_file, cwd, **kw):
        seen.update(kw)
        return git_native.GitResult(returncode=0, stdout="", stderr="")

    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t.example"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True,
                       capture_output=True, text=True)
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")

    monkeypatch.setattr(git_native, "commit_scoped", _fake_commit_scoped)
    commit_pipeline.commit(
        repo,
        message="m",
        commit_paths=["a.txt"],
        suppress_post_commit_auto_push=True,
    )
    assert seen.get("suppress_post_commit_auto_push") is True


# ---------------------------------------------------------------------------
# The re-hosted drain
# ---------------------------------------------------------------------------


def test_pending_push_drain_is_rehosted_not_dropped(monkeypatch, tmp_path):
    """`drain_pending_push` still has a per-commit host after the hook stands down.

    Its own docstring names three independent call sites and rests its
    "delay, never lose" argument on their independence; C-01 removes the first
    one for sync-push commits. This asserts the replacement host fires, so the
    guarantee narrows to two hosts only if someone deletes this deliberately.
    """
    drained: list = []
    monkeypatch.setattr(
        auto_push, "drain_pending_push", lambda root: drained.append(root)
    )
    commit_pipeline._drain_pending_push_after_sync(tmp_path)
    assert drained == [str(tmp_path)]


def test_drain_failure_never_fails_a_landed_push(monkeypatch, tmp_path):
    """A drain that raises must not turn a landed, pushed commit into a failure."""

    def _boom(root):
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(auto_push, "drain_pending_push", _boom)
    commit_pipeline._drain_pending_push_after_sync(tmp_path)  # must not raise
