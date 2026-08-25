"""
test_stale_index_rejection.py — the predicate behind
`scoped_git_commit._reject_stale_index_paths`, at fast-tier cost.

The end-to-end reproductions (a real repo, a real index made to hold the
pre-commit blob) live in `test_scoped_git_commit.py`, which is
`spawns_process`/`cadence`. This module stubs `git_native._git` instead, so the
decision rule itself — refuse iff a path's index differs from HEAD while its
worktree does NOT — is protected on every fast run, including the fail-open
posture on an unanswerable git.

Incident: state/bug-backlog/2026-08-19-shared-git-index-holds-stale-pre-head-
sn-b5b83e42e275.yaml, recurrence of 2026-08-20 (a54addce reverted cd751b79
through `session.safe_commit_offer`).
"""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.ops.ceremony import git_native, scoped_git_commit

PATHS = ["coordinator_core/ipc.py", "notes.md"]


def _stub_git(monkeypatch, *, worktree: str, cached: str, ok: bool = True):
    """Answer the two batched probes by which one is being run.

    Records every invocation so the batching contract (two calls, never one
    per path) is assertable — the amplification gate forbids the per-path
    probe shape the bug entry's own repro_steps use.
    """
    calls: list[list[str]] = []

    def _fake(args, *, cwd, **kwargs):
        calls.append(list(args))
        stdout = cached if "--cached" in args else worktree
        return SimpleNamespace(ok=ok, returncode=0 if ok else 128, stdout=stdout, stderr="")

    monkeypatch.setattr(git_native, "_git", _fake)
    return calls


def test_refuses_when_index_diverges_and_worktree_does_not(monkeypatch):
    _stub_git(monkeypatch, worktree="", cached="coordinator_core/ipc.py\n")
    reason = scoped_git_commit._reject_stale_index_paths(PATHS, "/repo")
    assert reason is not None
    assert "stale index" in reason
    assert "coordinator_core/ipc.py" in reason


def test_allows_when_both_diverge(monkeypatch):
    """An ordinary `git add` leaves worktree == index != HEAD."""
    _stub_git(
        monkeypatch,
        worktree="coordinator_core/ipc.py\n",
        cached="coordinator_core/ipc.py\n",
    )
    assert scoped_git_commit._reject_stale_index_paths(PATHS, "/repo") is None


def test_allows_an_unstaged_worktree_edit(monkeypatch):
    _stub_git(monkeypatch, worktree="notes.md\n", cached="")
    assert scoped_git_commit._reject_stale_index_paths(PATHS, "/repo") is None


def test_allows_a_clean_pathspec(monkeypatch):
    _stub_git(monkeypatch, worktree="", cached="")
    assert scoped_git_commit._reject_stale_index_paths(PATHS, "/repo") is None


def test_fails_open_when_git_cannot_answer(monkeypatch):
    """An unanswerable probe must not wedge every commit on the box; the
    stale-index shape is persistent, so the next call catches it."""
    _stub_git(monkeypatch, worktree="", cached="coordinator_core/ipc.py\n", ok=False)
    assert scoped_git_commit._reject_stale_index_paths(PATHS, "/repo") is None


def test_probe_count_is_two_regardless_of_path_count(monkeypatch):
    calls = _stub_git(monkeypatch, worktree="", cached="")
    many = ["f%d.py" % i for i in range(50)]
    scoped_git_commit._reject_stale_index_paths(many, "/repo")
    assert len(calls) == 2
    for args in calls:
        assert args[-50:] == many


def test_empty_pathspec_is_not_probed(monkeypatch):
    calls = _stub_git(monkeypatch, worktree="", cached="")
    assert scoped_git_commit._reject_stale_index_paths([], "/repo") is None
    assert calls == []


def test_message_switches_to_a_count_past_the_display_cap(monkeypatch):
    shown = scoped_git_commit._STALE_INDEX_PATHS_SHOWN
    stale = ["f%d.py" % i for i in range(shown + 3)]
    _stub_git(monkeypatch, worktree="", cached="\n".join(stale))
    reason = scoped_git_commit._reject_stale_index_paths(stale, "/repo")
    assert reason is not None
    assert "(+3 more)" in reason
    assert "%d path(s)" % len(stale) in reason
    assert reason.count(".py") == shown
