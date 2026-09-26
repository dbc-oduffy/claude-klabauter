
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.emit import enrich
from coordinator_core.win_portability import no_console_creationflags

# the batched walk agrees with the git-native oracle. The spawn ratchet's `_BASELINE`
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, **no_console_creationflags())


def _commit_date(repo: Path, rel_path: str) -> str | None:
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

    (tmp_path / "docs" / "a.md").write_text("a v2\n")
    _run_git(tmp_path, "add", "docs/a.md")
    _run_git(tmp_path, "commit", "-q", "-m", "update a")

    (tmp_path / "docs" / "untracked.md").write_text("never committed\n")

    return tmp_path


class TestBatchLastModifiedAt:
    def test_resolves_most_recent_commit_per_path(self, repo: Path) -> None:
        paths = ["docs/a.md", "docs/b.md"]
        result = enrich.batch_last_modified_at(repo, paths)

        assert result[0] == _commit_date(repo, "docs/a.md")
        assert result[1] == _commit_date(repo, "docs/b.md")
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
        assert result[0] is None
        assert result[1] == _commit_date(repo, "docs/a.md")
        assert result[2] is None
        assert result[3] == _commit_date(repo, "docs/b.md")
        assert result[4] is None

    def test_matches_old_per_path_spawn_for_every_path_in_repo(self, repo: Path) -> None:
        paths = ["docs/a.md", "docs/b.md", "docs/untracked.md", "docs/does-not-exist.md"]
        batched = enrich.batch_last_modified_at(repo, paths)
        oracle = [_commit_date(repo, p) for p in paths]
        assert batched == oracle


class TestBatchLastModifiedAtGrouped:

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
        groups = [["docs/a.md", "docs/untracked.md"], ["docs/b.md"]]
        grouped = enrich.batch_last_modified_at_grouped(repo, groups)
        assert grouped[0][0] == enrich.batch_last_modified_at(repo, ["docs/a.md"])[0]
        assert grouped[1][0] == enrich.batch_last_modified_at(repo, ["docs/b.md"])[0]


class TestWalkLastModifiedAt:

    def test_early_terminate_on_all_resolved(self, repo: Path) -> None:
        result = enrich._walk_last_modified_at(repo, {"docs/a.md"})
        assert result == {"docs/a.md": _commit_date(repo, "docs/a.md")}

    def test_empty_wanted_set_short_circuits(self, repo: Path) -> None:
        assert enrich._walk_last_modified_at(repo, set()) == {}

    def test_git_failure_returns_empty_dict(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        result = enrich._walk_last_modified_at(not_a_repo, {"docs/a.md"})
        assert result == {}


class TestMergeCommitEquivalence:

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
        other = repo / "docs" / "other.md"
        other.write_text("other content\n")
        _run_git(repo, "add", "docs/other.md")
        _run_git(repo, "commit", "-q", "-m", "touch other file on branch-a")

        _run_git(repo, "checkout", "-q", "-")
        f.write_text("b-change\n")
        _run_git(repo, "commit", "-q", "-am", "b change f.md")

        _run_git(repo, "merge", "--no-edit", "branch-a")

        oracle = _commit_date(repo, "docs/f.md")
        batched = enrich.batch_last_modified_at(repo, ["docs/f.md"])[0]
        assert batched == oracle
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
        assert oracle is not None

        batched = enrich.batch_last_modified_at(repo, [rel_path])[0]
        assert batched == oracle
        assert batched is not None
