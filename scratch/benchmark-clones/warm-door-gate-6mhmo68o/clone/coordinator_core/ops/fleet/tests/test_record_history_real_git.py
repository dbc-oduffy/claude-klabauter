"""
Real-git-worktree coverage for `fleet.record_history` (AC3).

Spec backlink: docs/plans/2026-08-20-a-counted-fleet-answer-for-record-history.md,
chunk C1, AC3.

Why this is a SEPARATE file from `test_record_history.py`: AC3's fixture needs a
worktree `git rev-parse --is-inside-work-tree` actually accepts, so this file
spawns real `git` processes. The spawn ratchet
(`coordinator_core/tests/test_no_new_spawning_tests.py`, Rules 2 and 4) requires
every spawning test file to declare `spawns_process` AND to be `cadence`-tiered,
which takes it off the fast tier. Splitting keeps C1's other coverage
(pass-through identity, up-front validation, handler wiring, spawn-budget proof)
on the fast tier instead of dragging all of it to cadence.

Negative-spec:
    - Does NOT construct the `git` argv dynamically. `["git", *args]` is a
      static-argv0 spawn site the ratchet can resolve; a `["git"] + list(args)`
      BinOp resolves to `<dynamic>` and is SKIPPED rather than guessed, which
      silently hides a real spawning file from Rules 2 and 4. See the
      bug-backlog entry for that collector blind spot.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import record_history as frh

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=True)


def _make_real_git_repo(tmp_path: Path, name: str = "git-repo") -> Path:
    """A REAL, `git init`'d worktree — `_is_git_worktree` spawns
    `git rev-parse --is-inside-work-tree`, so a directory-with-a-`.git`-folder
    fixture is not sufficient; this predicate needs a repo git itself accepts."""
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def _make_real_non_git_dir(tmp_path: Path, name: str = "non-git-dir") -> Path:
    """Exists, is a directory, is NOT a git worktree at all."""
    root = tmp_path / name
    root.mkdir()
    return root


def _make_record(root: Path, name: str, status: str) -> Path:
    subdir = root / "state" / "sizings"
    subdir.mkdir(parents=True, exist_ok=True)
    path = subdir / name
    path.write_text(f"---\nstatus: {status}\n---\nBody.\n", encoding="utf-8")
    return path


class TestSkippedRootLegExercisedForReal:
    """AC3: one real git worktree, one real-but-non-git directory —
    `_is_git_worktree` (the predicate under test) is exercised for real."""

    def test_git_worktree_walked_non_git_dir_skipped(self, tmp_path, monkeypatch) -> None:
        git_root = _make_real_git_repo(tmp_path, "git-repo")
        non_git_root = _make_real_non_git_dir(tmp_path, "non-git-dir")
        _make_record(git_root, "s-000001.md", "sized")

        monkeypatch.setattr(
            frh, "_resolve_active_sibling_paths", lambda: [git_root, non_git_root]
        )

        result = frh.build_fleet_record_history("sizing-object")

        assert result["queried_root_count"] == 1
        assert len(result["roots_walked"]) == 1
        assert git_root.as_posix() in result["roots_walked"]

        skipped_roots = {entry["root"] for entry in result["roots_skipped"]}
        assert non_git_root.as_posix() in skipped_roots

        assert git_root.as_posix() in result["repos"]
        assert non_git_root.as_posix() not in result["repos"]

    def test_queried_root_count_never_equals_the_candidate_total(
        self, tmp_path, monkeypatch
    ) -> None:
        """AC3's second half, stated as its own assertion: the count is over
        WALKED roots, so it must be strictly below the candidate total whenever
        any candidate was skipped. This is the leg that stops a caller
        presenting the number as a whole-fleet claim (AC10's over-claim bar)."""
        git_root = _make_real_git_repo(tmp_path, "git-repo")
        non_git_a = _make_real_non_git_dir(tmp_path, "non-git-a")
        non_git_b = _make_real_non_git_dir(tmp_path, "non-git-b")
        candidates = [git_root, non_git_a, non_git_b]

        monkeypatch.setattr(frh, "_resolve_active_sibling_paths", lambda: candidates)

        result = frh.build_fleet_record_history("sizing-object")

        assert result["queried_root_count"] == 1
        assert result["queried_root_count"] < len(candidates)
