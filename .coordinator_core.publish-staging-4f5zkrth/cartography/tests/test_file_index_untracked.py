"""
coordinator_core.cartography.tests.test_file_index_untracked

Dedicated coverage for `build_file_index`'s `include_untracked` opt-in arm
(C6, hard-requirement partial 2): a fresh input corpus is frequently
git-untracked, and `build_file_index`'s pre-existing behaviour (enumerate via
`cartography.tree.list_tracked_files`, i.e. `git ls-files`) silently omits
every untracked file — the worst failure shape for an indexer whose whole
job is completeness (state/dispatch-briefs/2026-08-21-engine-half-of-the-
roadmap-sprint-spine-split/C6.md).

Coverage:
  (a) FAILS-THE-WRONG-WAY-FIRST regression: an untracked file planted in the
      fixture is silently absent from `build_file_index`'s default output —
      pins the defect this arm exists to close.
  (b) `include_untracked=True` folds the untracked file into `index` and
      `systems` alongside the tracked corpus.
  (c) `list_untracked_files` itself, over a fixture with a nested untracked
      path.
  (d) A `.gitignore`-excluded file stays excluded even with the arm on
      (`--exclude-standard` semantics).

Spec backlink: pln-makima-cartography-substrate-a-26eb2e § C2;
state/dispatch-briefs/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split/C6.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.cartography.file_index import (
    REPO_ROOT_SYSTEM,
    build_file_index,
    list_untracked_files,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture()
def git_repo_with_untracked(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "cartography-test@makima.test")
    _git("config", "user.name", "Cartography Test")
    _git("config", "commit.gpgsign", "false")

    (root / "README.md").write_text("root file\n", encoding="utf-8")
    (root / "coordinator_core").mkdir()
    (root / "coordinator_core" / "y.py").write_text("pass\n", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "seed")

    # Untracked, non-ignored — a fresh input corpus file, staged not committed.
    (root / "coordinator_core" / "fresh.py").write_text("pass\n", encoding="utf-8")
    (root / "state").mkdir()
    (root / "state" / "new.yaml").write_text("k: v\n", encoding="utf-8")

    # gitignored — must stay excluded even with the arm on.
    (root / ".gitignore").write_text("ignored_scratch/\n", encoding="utf-8")
    _git("add", ".gitignore")
    _git("commit", "-m", "add gitignore")
    (root / "ignored_scratch").mkdir()
    (root / "ignored_scratch" / "noise.txt").write_text("noise\n", encoding="utf-8")

    return root


# ---------------------------------------------------------------------------
# (a) fails-the-wrong-way-first: default output silently omits untracked
# ---------------------------------------------------------------------------


def test_default_output_omits_untracked_file(git_repo_with_untracked):
    result = build_file_index(git_repo_with_untracked)
    assert "coordinator_core/fresh.py" not in result["index"]
    assert "state/new.yaml" not in result["index"]


# ---------------------------------------------------------------------------
# (b) include_untracked=True folds untracked files in
# ---------------------------------------------------------------------------


def test_include_untracked_true_folds_in_untracked_files(git_repo_with_untracked):
    result = build_file_index(git_repo_with_untracked, include_untracked=True)
    assert result["index"]["coordinator_core/fresh.py"] == "coordinator_core"
    assert result["index"]["state/new.yaml"] == "state"
    assert result["systems"]["coordinator_core"] == 2
    assert result["systems"]["state"] == 1
    assert result["unmapped_count"] == 0
    assert sum(result["systems"].values()) == result["file_count"]


def test_include_untracked_true_excludes_gitignored(git_repo_with_untracked):
    result = build_file_index(git_repo_with_untracked, include_untracked=True)
    assert "ignored_scratch/noise.txt" not in result["index"]


# ---------------------------------------------------------------------------
# (c) list_untracked_files
# ---------------------------------------------------------------------------


def test_list_untracked_files_returns_nested_untracked_paths(git_repo_with_untracked):
    untracked = list_untracked_files(git_repo_with_untracked)
    assert "coordinator_core/fresh.py" in untracked
    assert "state/new.yaml" in untracked
    assert "ignored_scratch/noise.txt" not in untracked
    # Tracked files are not re-listed as untracked.
    assert "README.md" not in untracked


def test_list_untracked_files_empty_when_tree_clean(tmp_path):
    root = tmp_path / "clean_repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True)

    _git("init", "-b", "main")
    _git("config", "user.email", "cartography-test@makima.test")
    _git("config", "user.name", "Cartography Test")
    _git("config", "commit.gpgsign", "false")
    (root / "README.md").write_text("root\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "seed")

    assert list_untracked_files(root) == []
