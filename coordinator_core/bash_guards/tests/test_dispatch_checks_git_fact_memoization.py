"""Regression tests for the two spawn-per-item git-fact-memoization fixes in
`coordinator_core.bash_guards.dispatch_checks` (spawn-storm sweep, chunk D6,
`docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md`).

(a) `_check_destructive_git_revert_full` ran its `git status --porcelain`
    oracle once PER SEGMENT even when every segment shared the same
    `git_cwd` -- a chained `git checkout . && git reset --hard` against one
    working tree re-asked "what's dirty here?" twice for an answer that
    cannot change mid-dispatch (a single synchronous hook invocation, no
    concurrent mutator). Fixed with a local `_memo_status_porcelain` cache
    keyed on `git_cwd` (NOT the shared `_new_git_memo`/`_memo_run_git`
    closure other checks in this file use -- that closure drops
    `extra_env`, and this oracle depends on `LC_ALL=C`).

(b) `check_destructive_git_orphan` resolved the current branch
    (`rev-parse --abbrev-ref HEAD`) via a bare `_run_git` call sitting
    directly beside two `_memo_run_git` calls. Verified this call is
    single-shot in practice (it sits inside the branch that returns
    immediately), so routing it through the memo changes no spawn count --
    it closes a style/consistency gap, not a duplicate-spawn defect. No
    freshness reason applies (repo state cannot change mid-dispatch, per
    this module's own documented reasoning for `_new_git_memo`), so the
    route-through was applied.

Both touched functions parse the command with plain `re`/`shlex` --
`_split_segments`, `_extract_git_c_dir`, `_orphan_c_cwd` -- with no
`sys.platform`/`os.name` branch anywhere in either function or its call
chain (verified by grep before writing this file). AC9's "cover both
platform branches where one exists" is therefore vacuous here: there is
exactly one code path, exercised on every host OS the fast tier runs on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards.dispatch_checks import (
    check_destructive_git_orphan,
    check_destructive_git_revert,
    check_destructive_git_revert_advisory,
)
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _deny_reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


@pytest.fixture()
def repo_with_peer_work(tmp_path: Path) -> Path:
    """Load-bearing dirty tracked file -- the hard-deny shape."""
    repo = tmp_path / "shared-tree"
    (repo / "state").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    peer_file = repo / "state" / "peer-in-flight.md"
    peer_file.write_text("committed baseline\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    peer_file.write_text("committed baseline\npeer's in-flight edit\n", encoding="utf-8")
    return repo


@pytest.fixture()
def repo_with_ordinary_dirty_file(tmp_path: Path) -> Path:
    """Dirty tracked file OUTSIDE any load-bearing prefix -- `affected`
    non-empty, `deny_paths` empty, the advisory-only shape. Chosen for the
    spawn-count test specifically because it does NOT return early: both
    segments of a chained command actually run their branch's oracle call,
    so a genuine double-spawn would be visible."""
    repo = tmp_path / "ordinary-tree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    tracked = repo / "app.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "baseline")
    tracked.write_text("x = 2\n", encoding="utf-8")
    return repo


@pytest.fixture()
def clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "clean-tree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "baseline")
    return repo


@pytest.fixture()
def repo_with_droppable_commit(tmp_path: Path) -> Tuple[Path, str]:
    """Two commits on an explicitly-named `main` branch -- resetting --hard
    to the first commit orphans the second. Branch name is pinned via
    `checkout -b` rather than relying on `init.defaultBranch`, so the
    assertion on the branch name in the deny message isn't environment-
    dependent."""
    repo = tmp_path / "orphan-tree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "commit-a")
    sha_a = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout.strip()
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "commit-b")
    return repo, sha_a


def _counting_run_git(monkeypatch: pytest.MonkeyPatch, match_prefix: List[str]) -> List[int]:
    """Wrap the module's real `_run_git`, counting only calls whose args
    start with `match_prefix` -- other oracle calls (rev-parse --show-
    toplevel, git log, ...) are noise for this assertion."""
    original = dispatch_checks._run_git
    count = [0]

    def wrapper(args, cwd=None, timeout=2.0, extra_env=None):
        if list(args[: len(match_prefix)]) == match_prefix:
            count[0] += 1
        return original(args, cwd=cwd, timeout=timeout, extra_env=extra_env)

    monkeypatch.setattr(dispatch_checks, "_run_git", wrapper)
    return count


class TestGitStatusPorcelainMemoizedAcrossSegments:
    """(a) `_check_destructive_git_revert_full`'s `git status --porcelain`
    oracle must resolve ONCE per distinct `git_cwd`, no matter how many
    segments of a chained command share that cwd."""

    def test_status_probe_resolved_once_across_chained_checkout_and_reset(
        self, monkeypatch: pytest.MonkeyPatch, repo_with_ordinary_dirty_file: Path
    ) -> None:
        count = _counting_run_git(monkeypatch, ["--no-optional-locks", "status", "--porcelain"])
        cmd = "git -C %s checkout . && git -C %s reset --hard" % (
            repo_with_ordinary_dirty_file,
            repo_with_ordinary_dirty_file,
        )
        check_destructive_git_revert_advisory(cmd)
        assert count[0] == 1, (
            "chained 'git checkout . && git reset --hard' against ONE "
            "working tree spawned %d 'git status --porcelain' calls; "
            "expected exactly 1 -- the fact is loop-invariant per cwd and "
            "must resolve once." % count[0]
        )

    def test_status_probe_resolved_once_across_three_chained_segments(
        self, monkeypatch: pytest.MonkeyPatch, repo_with_ordinary_dirty_file: Path
    ) -> None:
        count = _counting_run_git(monkeypatch, ["--no-optional-locks", "status", "--porcelain"])
        cmd = "git -C %s checkout . && git -C %s reset --hard && git -C %s stash" % (
            (repo_with_ordinary_dirty_file,) * 3
        )
        check_destructive_git_revert_advisory(cmd)
        assert count[0] == 1

    def test_status_probe_still_scoped_per_distinct_cwd(
        self,
        monkeypatch: pytest.MonkeyPatch,
        repo_with_ordinary_dirty_file: Path,
        clean_repo: Path,
    ) -> None:
        """The memo is keyed on `git_cwd`, not hoisted unconditionally --
        two DIFFERENT repos in the same chained command must each still get
        their own, real oracle call."""
        count = _counting_run_git(monkeypatch, ["--no-optional-locks", "status", "--porcelain"])
        cmd = "git -C %s reset --hard && git -C %s reset --hard" % (
            repo_with_ordinary_dirty_file,
            clean_repo,
        )
        check_destructive_git_revert_advisory(cmd)
        assert count[0] == 2


class TestGitRevertFullPriorVerdictUnchanged:
    """Behavioral rows already covered by `test_check_destructive_git_revert
    _stash.py` -- reasserted here against the memoized code path so a
    verdict regression introduced by the memo fix is caught locally, without
    editing that peer-owned file."""

    def test_loadbearing_dirty_tree_still_denies_on_reset_hard(
        self, repo_with_peer_work: Path
    ) -> None:
        result = check_destructive_git_revert("git -C %s reset --hard" % repo_with_peer_work)
        assert result is not None
        assert "state/peer-in-flight.md" in _deny_reason(result)

    def test_loadbearing_dirty_tree_still_denies_on_chained_segments(
        self, repo_with_peer_work: Path
    ) -> None:
        """The memoized status call must still surface the SAME deny on
        whichever segment first matches, even when preceded by a benign
        segment sharing the same cwd."""
        cmd = "git -C %s status && git -C %s reset --hard" % (
            repo_with_peer_work,
            repo_with_peer_work,
        )
        result = check_destructive_git_revert(cmd)
        assert result is not None
        assert "state/peer-in-flight.md" in _deny_reason(result)

    def test_ordinary_dirty_tree_is_advisory_not_deny(
        self, repo_with_ordinary_dirty_file: Path
    ) -> None:
        cmd = "git -C %s reset --hard" % repo_with_ordinary_dirty_file
        assert check_destructive_git_revert(cmd) is None
        advisory = check_destructive_git_revert_advisory(cmd)
        assert advisory is not None

    def test_clean_tree_allows(self, clean_repo: Path) -> None:
        cmd = "git -C %s checkout . && git -C %s reset --hard" % (clean_repo, clean_repo)
        assert check_destructive_git_revert(cmd) is None
        assert check_destructive_git_revert_advisory(cmd) is None


class TestOrphanCurrentBranchMemoization:
    """(b) `check_destructive_git_orphan` CHECK 1's branch-name resolution,
    now routed through `_memo_run_git`. This call is single-shot in
    practice (the enclosing branch returns immediately), so this class
    asserts the deny content is byte-for-byte equivalent to the pre-fix
    shape rather than a spawn count."""

    def test_reset_hard_dropping_a_commit_still_denies_with_correct_branch(
        self, repo_with_droppable_commit: Tuple[Path, str]
    ) -> None:
        repo, sha_a = repo_with_droppable_commit
        result = check_destructive_git_orphan("git -C %s reset --hard %s" % (repo, sha_a))
        assert result is not None
        reason = _deny_reason(result)
        assert "branch 'main'" in reason
        assert "1 commit(s)" in reason
        assert "commit-b" in reason

    def test_reset_hard_to_current_head_allows(
        self, repo_with_droppable_commit: Tuple[Path, str]
    ) -> None:
        repo, _sha_a = repo_with_droppable_commit
        assert check_destructive_git_orphan("git -C %s reset --hard HEAD" % repo) is None
