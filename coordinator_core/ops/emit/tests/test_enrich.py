"""Tests for coordinator_core.ops.emit.enrich — batched last-modified-at derivation.

Locks the single-walk batching behaviour (2026-07-17 perf rewrite: one ``git log
--name-only`` walk instead of one ``git log -1`` subprocess per path) against:
  - correct resolution of the most-recent-commit committer date per path,
  - the no-history / untracked-path fallback (``None``), unchanged from the old
    per-path-spawn behaviour,
  - input-order preservation in the returned list,
  - empty-path handling (empty string in ``paths`` maps to ``None`` without a git call).

Spec backlink: emit.cadence O(corpus) git-spawn-per-path fix, 2026-07-17.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.emit import enrich
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: this file spawns real git because the property under test
# IS git's own `git log --name-only`/`-1 --format=%cI` history-walk semantics --
# merge-commit tree-simplification equivalence (TestMergeCommitEquivalence) and
# `core.quotepath` non-ASCII filename handling (TestNonAsciiFilenameEquivalence) are
# real git behaviours no mock can stand in for; the whole file's purpose is proving
# the batched walk agrees with the git-native oracle. The spawn ratchet's `_BASELINE`
# is shrink-only pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, **no_console_creationflags())


def _commit_date(repo: Path, rel_path: str) -> str | None:
    """Oracle: the old per-path ``git log -1 --format=%cI -- <path>`` behaviour."""
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%cI", "--", rel_path],
        capture_output=True,
        text=True,
        check=False,
        **no_console_creationflags(),
    )
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny real git repo with a few paths committed at different times, plus an
    untracked path never committed — matches the shape of the live docs/plans/ corpus.
    """
    _run_git(tmp_path, "init", "-q")
    _run_git(tmp_path, "config", "user.email", "test@example.com")
    _run_git(tmp_path, "config", "user.name", "Test")

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("a v1\n")
    _run_git(tmp_path, "add", "docs/a.md")
    _run_git(tmp_path, "commit", "-q", "-m", "add a")

    (tmp_path / "docs" / "b.md").write_text("b v1\n")
    _run_git(tmp_path, "add", "docs/b.md")
    _run_git(tmp_path, "commit", "-q", "-m", "add b")

    # a.md touched again in a later commit — its LMA must reflect THIS commit, not the
    # first one (proves "first record seen in newest-first walk wins").
    (tmp_path / "docs" / "a.md").write_text("a v2\n")
    _run_git(tmp_path, "add", "docs/a.md")
    _run_git(tmp_path, "commit", "-q", "-m", "update a")

    # never committed — the no-history fallback case.
    (tmp_path / "docs" / "untracked.md").write_text("never committed\n")

    return tmp_path


class TestBatchLastModifiedAt:
    def test_resolves_most_recent_commit_per_path(self, repo: Path) -> None:
        paths = ["docs/a.md", "docs/b.md"]
        result = enrich.batch_last_modified_at(repo, paths)

        assert result[0] == _commit_date(repo, "docs/a.md")
        assert result[1] == _commit_date(repo, "docs/b.md")
        # a.md was touched by the LATEST commit overall — its LMA must be newer than b.md's.
        assert result[0] is not None and result[1] is not None
        assert result[0] >= result[1]

    def test_no_history_path_falls_back_to_none(self, repo: Path) -> None:
        result = enrich.batch_last_modified_at(repo, ["docs/untracked.md"])
        assert result == [None]
        assert _commit_date(repo, "docs/untracked.md") is None

    def test_nonexistent_path_falls_back_to_none(self, repo: Path) -> None:
        result = enrich.batch_last_modified_at(repo, ["docs/does-not-exist.md"])
        assert result == [None]

    def test_empty_string_path_is_none_without_git_call(self, repo: Path) -> None:
        result = enrich.batch_last_modified_at(repo, [""])
        assert result == [None]

    def test_empty_paths_list_returns_empty_list(self, repo: Path) -> None:
        assert enrich.batch_last_modified_at(repo, []) == []

    def test_input_order_preserved_with_mixed_hits_and_misses(self, repo: Path) -> None:
        paths = ["docs/untracked.md", "docs/a.md", "", "docs/b.md", "docs/does-not-exist.md"]
        result = enrich.batch_last_modified_at(repo, paths)

        assert len(result) == len(paths)
        assert result[0] is None  # untracked
        assert result[1] == _commit_date(repo, "docs/a.md")
        assert result[2] is None  # empty string
        assert result[3] == _commit_date(repo, "docs/b.md")
        assert result[4] is None  # nonexistent

    def test_matches_old_per_path_spawn_for_every_path_in_repo(self, repo: Path) -> None:
        """Direct equivalence oracle: batched result must match the per-path
        ``git log -1`` spawn for every real path in this fixture's corpus."""
        paths = ["docs/a.md", "docs/b.md", "docs/untracked.md", "docs/does-not-exist.md"]
        batched = enrich.batch_last_modified_at(repo, paths)
        oracle = [_commit_date(repo, p) for p in paths]
        assert batched == oracle


class TestBatchLastModifiedAtGrouped:
    """PERF 2026-07-29: one shared git-log walk across multiple record groups (envelope's
    handoffs/plans/roadmaps), replacing one walk per group. Locks equivalence to calling
    ``batch_last_modified_at`` once per group, plus per-group input-order isolation."""

    def test_matches_per_group_batch_calls(self, repo: Path) -> None:
        groups = [
            ["docs/a.md", "docs/untracked.md"],
            ["docs/b.md", "docs/does-not-exist.md", "docs/a.md"],
        ]
        grouped = enrich.batch_last_modified_at_grouped(repo, groups)
        separate = [enrich.batch_last_modified_at(repo, g) for g in groups]
        assert grouped == separate

    def test_each_group_keeps_its_own_input_order(self, repo: Path) -> None:
        groups = [["docs/b.md", "docs/a.md"], ["docs/a.md", "docs/b.md"]]
        grouped = enrich.batch_last_modified_at_grouped(repo, groups)
        # Same two paths, opposite order per group -- results must be independently ordered.
        assert grouped[0] == list(reversed(grouped[1]))

    def test_shared_path_across_groups_resolves_identically_in_each(self, repo: Path) -> None:
        groups = [["docs/a.md"], ["docs/a.md", "docs/b.md"]]
        grouped = enrich.batch_last_modified_at_grouped(repo, groups)
        assert grouped[0][0] == grouped[1][0] == _commit_date(repo, "docs/a.md")

    def test_empty_groups_list_returns_empty_list(self, repo: Path) -> None:
        assert enrich.batch_last_modified_at_grouped(repo, []) == []

    def test_group_with_empty_paths_list_returns_empty_result_list(self, repo: Path) -> None:
        grouped = enrich.batch_last_modified_at_grouped(repo, [[], ["docs/a.md"]])
        assert grouped[0] == []
        assert grouped[1] == [_commit_date(repo, "docs/a.md")]

    def test_single_shared_walk_matches_solo_walk_per_path(self, repo: Path) -> None:
        """Correctness invariant the merge relies on: a path's resolved LMA depends only
        on its own history, never on what else is being walked alongside it."""
        groups = [["docs/a.md", "docs/untracked.md"], ["docs/b.md"]]
        grouped = enrich.batch_last_modified_at_grouped(repo, groups)
        assert grouped[0][0] == enrich.batch_last_modified_at(repo, ["docs/a.md"])[0]
        assert grouped[1][0] == enrich.batch_last_modified_at(repo, ["docs/b.md"])[0]


class TestWalkLastModifiedAt:
    """Direct coverage of the internal single-walk helper."""

    def test_early_terminate_on_all_resolved(self, repo: Path) -> None:
        """Requesting only the newest-touched path resolves via the walk without error,
        even though ``proc.terminate()`` fires before the full history is consumed."""
        result = enrich._walk_last_modified_at(repo, {"docs/a.md"})
        assert result == {"docs/a.md": _commit_date(repo, "docs/a.md")}

    def test_empty_wanted_set_short_circuits(self, repo: Path) -> None:
        assert enrich._walk_last_modified_at(repo, set()) == {}

    def test_git_failure_returns_empty_dict(self, tmp_path: Path) -> None:
        """A non-git directory yields an empty resolved dict (git exits non-zero); the
        caller (batch_last_modified_at) applies the None fallback for every path."""
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        result = enrich._walk_last_modified_at(not_a_repo, {"docs/a.md"})
        assert result == {}


class TestMergeCommitEquivalence:
    """Regression coverage for the merge-commit silent-drift finding (review 2026-07-17,
    Finding 1): plain ``git log --name-only`` (no ``-m``/``--cc``) suppresses diff output for
    merge commits entirely, so a merge that is the true last-touching commit for a path was
    invisible to the old walk and it fell back to an OLDER, wrong ancestor. The fix adds
    ``--cc`` to the walk so a merge whose resolution differs from ALL parents (i.e. is NOT
    treesame to any parent) surfaces its path — the same predicate the oracle's default
    history simplification uses to decide whether to keep a merge commit.
    """

    def test_conflicting_merge_resolution_matches_oracle(self, tmp_path: Path) -> None:
        """A merge that resolves a real conflict to content DIFFERENT from both parents is
        NOT treesame to either parent for the path — the oracle keeps this merge commit as
        the path's last-modified answer, and the batched walk (with --cc) must agree."""
        repo = tmp_path
        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@example.com")
        _run_git(repo, "config", "user.name", "Test")

        (repo / "docs").mkdir()
        f = repo / "docs" / "f.md"
        f.write_text("base\n")
        _run_git(repo, "add", "docs/f.md")
        _run_git(repo, "commit", "-q", "-m", "base")

        _run_git(repo, "branch", "branch-a")
        _run_git(repo, "checkout", "-q", "branch-a")
        f.write_text("a-change\n")
        _run_git(repo, "commit", "-q", "-am", "a change")

        _run_git(repo, "checkout", "-q", "-")
        f.write_text("b-change\n")
        _run_git(repo, "commit", "-q", "-am", "b change")

        # merge branch-a: real conflict on docs/f.md, resolve it to content that
        # differs from BOTH parents.
        subprocess.run(
            ["git", "-C", str(repo), "merge", "--no-edit", "branch-a"],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
        f.write_text("merge-resolution-differs-from-both-parents\n")
        _run_git(repo, "add", "docs/f.md")
        _run_git(repo, "commit", "-q", "-m", "merge resolving conflict")

        oracle = _commit_date(repo, "docs/f.md")
        batched = enrich.batch_last_modified_at(repo, ["docs/f.md"])[0]
        assert batched == oracle

    def test_clean_merge_treesame_to_one_parent_matches_oracle(self, tmp_path: Path) -> None:
        """A clean, non-conflicting merge whose content for the path is IDENTICAL to one
        parent (treesame to that parent) is transparently SKIPPED by the oracle's history
        simplification, which resolves to that parent's touching commit instead. The batched
        walk (with --cc, which hides a path present-but-identical-in-at-least-one-parent)
        must agree — this is exactly the case ``-m`` gets wrong (it would still surface the
        merge commit, since the diff relative to the OTHER parent is non-empty)."""
        repo = tmp_path
        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@example.com")
        _run_git(repo, "config", "user.name", "Test")

        (repo / "docs").mkdir()
        f = repo / "docs" / "f.md"
        f.write_text("base\n")
        _run_git(repo, "add", "docs/f.md")
        _run_git(repo, "commit", "-q", "-m", "base")

        _run_git(repo, "branch", "branch-a")
        _run_git(repo, "checkout", "-q", "branch-a")
        # branch-a never touches docs/f.md -- it touches an unrelated file.
        other = repo / "docs" / "other.md"
        other.write_text("other content\n")
        _run_git(repo, "add", "docs/other.md")
        _run_git(repo, "commit", "-q", "-m", "touch other file on branch-a")

        _run_git(repo, "checkout", "-q", "-")
        f.write_text("b-change\n")
        _run_git(repo, "commit", "-q", "-am", "b change f.md")

        # merges cleanly -- branch-a never touched docs/f.md, so the merge's tree
        # for docs/f.md is identical (treesame) to the "b change" parent.
        _run_git(repo, "merge", "--no-edit", "branch-a")

        oracle = _commit_date(repo, "docs/f.md")
        batched = enrich.batch_last_modified_at(repo, ["docs/f.md"])[0]
        assert batched == oracle
        # sanity: the oracle did NOT resolve to the merge commit itself.
        merge_hash = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%H"],
            capture_output=True,
            text=True,
            check=True,
            **no_console_creationflags(),
        ).stdout.strip()
        oracle_hash = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%H", "--", "docs/f.md"],
            capture_output=True,
            text=True,
            check=True,
            **no_console_creationflags(),
        ).stdout.strip()
        assert oracle_hash != merge_hash


class TestNonAsciiFilenameEquivalence:
    """Regression coverage for the non-ASCII-filename silent-drift finding (review
    2026-07-17, Finding 2): ``git log --name-only`` quotes/escapes non-ASCII path bytes under
    git's default ``core.quotepath=true``, so the parser's bare string comparison against the
    raw input path never matched and the path silently fell back to the ``None`` "no history"
    sentinel. The fix passes ``-c core.quotepath=false`` to the walk.
    """

    def test_accented_filename_resolves_not_none(self, tmp_path: Path) -> None:
        repo = tmp_path
        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@example.com")
        _run_git(repo, "config", "user.name", "Test")

        (repo / "docs").mkdir()
        rel_path = "docs/café.md"
        f = repo / "docs" / "café.md"
        f.write_text("content\n", encoding="utf-8")
        _run_git(repo, "add", rel_path)
        _run_git(repo, "commit", "-q", "-m", "add café")

        oracle = _commit_date(repo, rel_path)
        assert oracle is not None  # sanity: the file genuinely has history

        batched = enrich.batch_last_modified_at(repo, [rel_path])[0]
        assert batched == oracle
        assert batched is not None
