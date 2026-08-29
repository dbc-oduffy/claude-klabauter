"""Tier T tests for `coordinator_core.ops.push_outstanding`.

Scope: this chunk's own writes (`push_outstanding.py` + this file). Uses real
`git` repos on disk (a work/*-named local branch, an on-disk bare "remote")
so the sha comparison exercises actual `.git/refs/remotes/<remote>/<branch>`
and `.git/packed-refs` content -- no mocking of `git_state`'s file reads,
since those are exactly what this module is trusted to get right.
`push_with_retry` itself IS monkeypatched at the delegation boundary, per
the brief's "reusing push_with_retry" contract -- this file is not re-testing
that function's own retry/branch-gate behavior.

C1b (AC7b) additions below cover the range-touches-LFS-paths predicate's
three arms directly (`_gitattributes_declares_lfs_filter`,
`_range_touches_lfs_paths`) plus the `GIT_LFS_SKIP_PUSH` env behaviour at
the `push_outstanding` delegation boundary -- including a repo fixture that
DOES track an LFS path, so arm 3 is exercised rather than assumed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.push_outstanding as push_outstanding_mod
from coordinator_core.ops.ceremony.push import PushOutcome
from coordinator_core.ops.push_outstanding import (
    _gitattributes_declares_lfs_filter,
    _range_touches_lfs_paths,
    push_outstanding,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # popup-intentional-last-resort
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=no_window,
    )


def _git_stdout(args, cwd) -> str:
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # popup-intentional-last-resort
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=no_window,
    )
    return result.stdout


def _init_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _make_repo_with_remote(tmp_path: Path, *, branch: str = "work/some-branch") -> Path:
    """A local repo, on `branch`, with a bare "remote" cloned-from origin and
    a matching `refs/remotes/origin/<branch>` tracking ref -- HEAD and the
    tracking ref start EQUAL (nothing outstanding)."""
    bare = tmp_path / "bare.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)

    repo = _init_repo(tmp_path, "repo")
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", branch], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", branch], repo)
    return repo


def test_nothing_outstanding_when_head_matches_upstream(tmp_path):
    """HEAD sha == `refs/remotes/origin/<branch>` sha -> zero-spawn skip,
    `push_with_retry` never invoked."""
    repo = _make_repo_with_remote(tmp_path)

    outcome = push_outstanding(repo)

    assert outcome.exit_code == 0
    assert outcome.skipped == ["push:nothing-outstanding"]
    assert outcome.acted == []


def test_outstanding_commit_delegates_to_push_with_retry(monkeypatch, tmp_path):
    """HEAD ahead of its upstream tracking ref -> delegates to
    `push_with_retry`, unmodified, with the same worktree root and kwargs."""
    repo = _make_repo_with_remote(tmp_path)
    _seed_file(repo, "second.txt", "more")
    _git(["add", "--", "second.txt"], repo)
    _git(["commit", "-q", "-m", "second"], repo)

    calls = []

    def _fake_push_with_retry(root, **kwargs):
        calls.append((Path(root), kwargs))
        return PushOutcome(exit_code=0, acted=["push"])

    monkeypatch.setattr(push_outstanding_mod, "push_with_retry", _fake_push_with_retry)

    outcome = push_outstanding(repo)

    assert outcome.acted == ["push"]
    assert len(calls) == 1
    called_root, called_kwargs = calls[0]
    assert called_root == repo
    assert called_kwargs == {
        "allow_protected_branch": False,
        "protected_branch_override_reason": None,
        # The ladder's own deadline (2026-08-26) is part of this delegation
        # contract, not an incidental kwarg: without it the push/fetch/rebase
        # ladder is bounded only by the wall-clock dispatch guard, which can
        # fire only mid-leg and yields an `unconfirmed` push instead of a
        # decided one. Pinned here so dropping it fails loudly.
        "budget_secs": push_outstanding_mod.PUSH_RETRY_BUDGET_SECS,
    }


def test_no_upstream_ref_reads_as_outstanding_not_nothing_to_do(monkeypatch, tmp_path):
    """A fresh branch with NO upstream tracking ref at all -- the
    first-push caveat -- must delegate to `push_with_retry`, never return
    the `push:nothing-outstanding` skip. Silently never publishing a new
    branch is the failure mode this asserts against."""
    repo = _init_repo(tmp_path, "repo")
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/brand-new"], repo)
    # No remote configured at all, and definitely no refs/remotes/*/work/brand-new.

    calls = []

    def _fake_push_with_retry(root, **kwargs):
        calls.append(root)
        return PushOutcome(exit_code=0, skipped=["push:no-remote"])

    monkeypatch.setattr(push_outstanding_mod, "push_with_retry", _fake_push_with_retry)

    outcome = push_outstanding(repo)

    assert len(calls) == 1
    assert outcome.skipped == ["push:no-remote"]


def test_packed_refs_fallback_used_when_loose_ref_absent(monkeypatch, tmp_path):
    """A loose `refs/remotes/origin/<branch>` ref that has been packed away
    (loose file absent, `packed-refs` carries the entry) must still be found
    -- and must still compare equal to HEAD to report nothing outstanding."""
    repo = _make_repo_with_remote(tmp_path, branch="work/pack-me")
    _git(["pack-refs", "--all"], repo)

    loose_ref = repo / ".git" / "refs" / "remotes" / "origin" / "work" / "pack-me"
    assert not loose_ref.exists()
    packed = (repo / ".git" / "packed-refs").read_text(encoding="utf-8")
    assert "refs/remotes/origin/work/pack-me" in packed

    outcome = push_outstanding(repo)

    assert outcome.exit_code == 0
    assert outcome.skipped == ["push:nothing-outstanding"]


def test_unresolvable_head_delegates_to_push_with_retry(monkeypatch, tmp_path):
    """Detached HEAD (no branch name to compare with) cannot perform the
    zero-spawn decision -- must fall through to `push_with_retry`, which
    resolves branch-unresolvability itself, rather than silently skipping
    the outstanding-work decision."""
    repo = _make_repo_with_remote(tmp_path)
    _git(["checkout", "--detach", "-q", "HEAD"], repo)

    calls = []

    def _fake_push_with_retry(root, **kwargs):
        calls.append(root)
        return PushOutcome(exit_code=0, skipped=["push:branch-unresolvable"])

    monkeypatch.setattr(push_outstanding_mod, "push_with_retry", _fake_push_with_retry)

    outcome = push_outstanding(repo)

    assert len(calls) == 1
    assert outcome.skipped == ["push:branch-unresolvable"]


def test_allow_protected_branch_kwargs_pass_through(monkeypatch, tmp_path):
    """The override kwargs must reach `push_with_retry` verbatim when the
    caller supplies them -- no silent default substitution."""
    repo = _make_repo_with_remote(tmp_path)
    _seed_file(repo, "second.txt", "more")
    _git(["add", "--", "second.txt"], repo)
    _git(["commit", "-q", "-m", "second"], repo)

    calls = []

    def _fake_push_with_retry(root, **kwargs):
        calls.append(kwargs)
        return PushOutcome(exit_code=0, acted=["push"])

    monkeypatch.setattr(push_outstanding_mod, "push_with_retry", _fake_push_with_retry)

    push_outstanding(repo, allow_protected_branch=True, protected_branch_override_reason="release")

    assert calls == [
        {
            "allow_protected_branch": True,
            "protected_branch_override_reason": "release",
            "budget_secs": push_outstanding_mod.PUSH_RETRY_BUDGET_SECS,
        }
    ]


# --- C1b (AC7b): range-touches-LFS-paths predicate -------------------------


def test_gitattributes_declares_lfs_filter_false_when_absent(tmp_path):
    """Arm 1, absent case: no `.gitattributes` at all -> `False`, zero spawns."""
    repo = _init_repo(tmp_path, "repo")
    assert _gitattributes_declares_lfs_filter(repo) is False


def test_gitattributes_declares_lfs_filter_false_when_no_filter_lfs_line(tmp_path):
    """Arm 1, present-but-clean case: a `.gitattributes` with content but no
    `filter=lfs` entry -> `False`, matching this repo's own real disposition."""
    repo = _init_repo(tmp_path, "repo")
    _seed_file(repo, ".gitattributes", "*.md text\n# filter=lfs in a comment does not count\n")
    assert _gitattributes_declares_lfs_filter(repo) is False


def test_gitattributes_declares_lfs_filter_true_when_declared(tmp_path):
    """Arm 1, positive case: a real `filter=lfs` line -> `True`."""
    repo = _init_repo(tmp_path, "repo")
    _seed_file(repo, ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    assert _gitattributes_declares_lfs_filter(repo) is True


def test_range_touches_lfs_paths_arm1_short_circuit_no_spawns(tmp_path):
    """Arm 1: no `filter=lfs` declared at all -> `False` without ever
    spawning `git diff`/`git check-attr` -- proven by seeding shas that are
    not even valid revisions; a spawn would fail loudly instead of the
    function short-circuiting first."""
    repo = _init_repo(tmp_path, "repo")
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    result = _range_touches_lfs_paths(repo, "not-a-real-sha", "also-not-real")

    assert result is False


def test_range_touches_lfs_paths_arm2_ordinary_paths_only(tmp_path):
    """Arm 2: `.gitattributes` declares `filter=lfs` (for a different
    pattern), but this range only touches an ordinary text path -> `False`,
    the batched `check-attr --stdin` call answering "not LFS" for it."""
    repo = _init_repo(tmp_path, "repo")
    _seed_file(repo, ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    _git(["add", "--", ".gitattributes"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    base_sha = _git_stdout(["rev-parse", "HEAD"], repo).strip()

    _seed_file(repo, "notes.txt", "ordinary text content")
    _git(["add", "--", "notes.txt"], repo)
    _git(["commit", "-q", "-m", "add notes"], repo)
    head_sha_value = _git_stdout(["rev-parse", "HEAD"], repo).strip()

    result = _range_touches_lfs_paths(repo, base_sha, head_sha_value)

    assert result is False


def test_range_touches_lfs_paths_arm3_lfs_tracked_path_present(tmp_path):
    """Arm 3: `.gitattributes` declares `filter=lfs` for `*.bin`, and the
    range under test adds a `*.bin` path -> `True`. This is the repo
    fixture that DOES track an LFS path, exercising arm 3 rather than
    assuming it."""
    repo = _init_repo(tmp_path, "repo")
    _seed_file(repo, ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    _git(["add", "--", ".gitattributes"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    base_sha = _git_stdout(["rev-parse", "HEAD"], repo).strip()

    _seed_file(repo, "asset.bin", "pretend binary payload")
    _git(["add", "--", "asset.bin"], repo)
    _git(["commit", "-q", "-m", "add binary asset"], repo)
    head_sha_value = _git_stdout(["rev-parse", "HEAD"], repo).strip()

    result = _range_touches_lfs_paths(repo, base_sha, head_sha_value)

    assert result is True


def test_push_outstanding_never_sets_git_lfs_skip_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`push_outstanding` must NEVER set `GIT_LFS_SKIP_PUSH` around the
    delegated push, and must record the range verdict instead.

    An earlier implementation set it for the duration of the
    `push_with_retry` call when the predicate reported a clean range. It was
    removed during execution, 2026-08-25, for three reasons -- see this
    module's own docstring. The one that makes this a REGRESSION TEST rather
    than a preference: `os.environ` is process-global, this engine is warm and
    serves 50-70 concurrent sessions, and a peer's push overlapping that window
    would inherit the variable and strand LFS objects it knew nothing about.
    Re-introducing the env-var arm must fail here loudly.
    """
    repo = _make_repo_with_remote(tmp_path)
    _seed_file(repo, "note.txt", "outstanding work")
    _git(["add", "--", "note.txt"], repo)
    _git(["commit", "-q", "-m", "outstanding"], repo)
    observed: dict[str, object] = {}

    def _fake_push_with_retry(root, **kwargs):  # noqa: ANN001, ANN003, ARG001
        observed["value"] = os.environ.get("GIT_LFS_SKIP_PUSH")
        return PushOutcome(exit_code=0, acted=["push"])

    monkeypatch.setattr(push_outstanding_mod, "push_with_retry", _fake_push_with_retry)
    before = os.environ.get("GIT_LFS_SKIP_PUSH")

    outcome = push_outstanding(repo)

    assert outcome.acted == ["push"]
    assert observed["value"] == before, (
        "push_outstanding changed GIT_LFS_SKIP_PUSH for the duration of the delegated push. "
        "That variable is process-global and this engine is shared across ~50-70 concurrent "
        "sessions, so a peer's overlapping push inherits it and strands its LFS objects. The "
        "range verdict is recorded on PushOutcome.skipped instead -- see the module docstring."
    )
    assert os.environ.get("GIT_LFS_SKIP_PUSH") == before
    assert "push:lfs-range-clean" in outcome.skipped


def test_push_outstanding_does_not_skip_lfs_when_range_touches_lfs_path(monkeypatch, tmp_path):
    """An outstanding range that DOES touch an LFS-tracked path (arm 3) must
    push normally -- `GIT_LFS_SKIP_PUSH` never set for the delegated call."""
    repo = _make_repo_with_remote(tmp_path)
    _seed_file(repo, ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    _git(["add", "--", ".gitattributes"], repo)
    _git(["commit", "-q", "-m", "add gitattributes"], repo)
    _git(["push", "-q"], repo)

    _seed_file(repo, "asset.bin", "pretend binary payload")
    _git(["add", "--", "asset.bin"], repo)
    _git(["commit", "-q", "-m", "add binary asset"], repo)

    monkeypatch.delenv("GIT_LFS_SKIP_PUSH", raising=False)

    observed = {}

    def _fake_push_with_retry(root, **kwargs):
        observed["value"] = os.environ.get("GIT_LFS_SKIP_PUSH")
        return PushOutcome(exit_code=0, acted=["push"])

    monkeypatch.setattr(push_outstanding_mod, "push_with_retry", _fake_push_with_retry)

    outcome = push_outstanding(repo)

    assert outcome.acted == ["push"]
    assert observed["value"] is None
    assert "GIT_LFS_SKIP_PUSH" not in os.environ


def test_push_outstanding_no_upstream_skips_predicate_pushes_normally(monkeypatch, tmp_path):
    """A fresh branch with no upstream tracking ref cannot cheaply establish
    a range -- the LFS predicate is skipped entirely (never evaluated,
    never sets `GIT_LFS_SKIP_PUSH`), falling straight through to
    `push_with_retry` exactly as the pre-existing no-upstream test asserts
    at the outstanding-work layer."""
    repo = _init_repo(tmp_path, "repo")
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/brand-new"], repo)

    monkeypatch.delenv("GIT_LFS_SKIP_PUSH", raising=False)

    observed = {}

    def _fake_push_with_retry(root, **kwargs):
        observed["value"] = os.environ.get("GIT_LFS_SKIP_PUSH")
        return PushOutcome(exit_code=0, skipped=["push:no-remote"])

    monkeypatch.setattr(push_outstanding_mod, "push_with_retry", _fake_push_with_retry)

    outcome = push_outstanding(repo)

    assert outcome.skipped == ["push:no-remote"]
    assert observed["value"] is None
    assert "GIT_LFS_SKIP_PUSH" not in os.environ
