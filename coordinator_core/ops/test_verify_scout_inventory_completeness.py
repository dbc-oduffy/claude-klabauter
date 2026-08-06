"""
Tests for coordinator_core.ops.verify_scout_inventory_completeness —
"research.verify_scout_inventory_completeness" op.

Covers the pure existence + min-line-count contract over a throwaway
tasks/**/scratch/ tree, the common_dir → worktree derivation via a real
tmp_path git repo (never against the working repo), the structured-error
premises (malformed expected_files / min_lines), and the AC7
double-invocation proof.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops import verify_scout_inventory_completeness as mod


def _git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )  # popup-safe-env-suppressed


@pytest.fixture
def repo(tmp_path):
    """Throwaway git repo whose .git dir is the engine-supplied common_dir.

    Seeds tasks/demo/scratch/ with one 40-line "complete" inventory file and
    one 5-line "short" inventory file; a third expected entry is never
    written at all (the "missing" case).
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@test.invalid")
    _git(root, "config", "user.name", "test")

    scratch = root / "tasks" / "demo" / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "complete.md").write_text("line\n" * 40, encoding="utf-8")
    (scratch / "short.md").write_text("line\n" * 5, encoding="utf-8")

    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "seed")
    return root


def _expected(names):
    return [f"tasks/demo/scratch/{name}" for name in names]


# ---------------------------------------------------------------------------
# Pure function — direct worktree arg
# ---------------------------------------------------------------------------


def test_all_present_and_long_enough_is_complete(repo):
    result = mod.verify_scout_inventory_completeness(
        _expected(["complete.md"]), worktree=repo
    )
    assert result == {"complete": True, "missing": [], "short": []}


def test_missing_file_is_reported(repo):
    result = mod.verify_scout_inventory_completeness(
        _expected(["complete.md", "absent.md"]), worktree=repo
    )
    assert result == {
        "complete": False,
        "missing": ["tasks/demo/scratch/absent.md"],
        "short": [],
    }


def test_short_file_is_reported(repo):
    result = mod.verify_scout_inventory_completeness(
        _expected(["complete.md", "short.md"]), worktree=repo
    )
    assert result == {
        "complete": False,
        "missing": [],
        "short": ["tasks/demo/scratch/short.md"],
    }


def test_directory_at_expected_path_counts_as_missing(repo):
    result = mod.verify_scout_inventory_completeness(
        _expected(["complete.md"]) + ["tasks/demo/scratch"], worktree=repo
    )
    assert result["missing"] == ["tasks/demo/scratch"]


def test_custom_min_lines_threshold(repo):
    result = mod.verify_scout_inventory_completeness(
        _expected(["short.md"]), min_lines=3, worktree=repo
    )
    assert result == {"complete": True, "missing": [], "short": []}


def test_absolute_path_entry_resolves_outside_worktree(repo, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("line\n" * 40, encoding="utf-8")
    result = mod.verify_scout_inventory_completeness([str(outside)], worktree=repo)
    assert result == {"complete": True, "missing": [], "short": []}


def test_empty_expected_files_is_vacuously_complete(repo):
    result = mod.verify_scout_inventory_completeness([], worktree=repo)
    assert result == {"complete": True, "missing": [], "short": []}


def test_double_invocation_identical_results(repo):
    """AC7: pure read — two back-to-back calls with identical inputs against
    an unchanged tree return identical results."""
    expected = _expected(["complete.md", "short.md", "absent.md"])
    first = mod.verify_scout_inventory_completeness(expected, worktree=repo)
    second = mod.verify_scout_inventory_completeness(expected, worktree=repo)
    assert first == second


# ---------------------------------------------------------------------------
# Handler — params validation + common_dir -> worktree derivation
# ---------------------------------------------------------------------------


def test_op_registered_and_handler_contract(repo):
    handler = get_op_handler("research.verify_scout_inventory_completeness")
    assert handler is not None
    common_dir = repo / ".git"
    result = handler(
        {"expected_files": _expected(["complete.md", "short.md"])}, common_dir
    )
    assert result == {
        "complete": False,
        "missing": [],
        "short": ["tasks/demo/scratch/short.md"],
    }


def test_handler_repo_root_none_falls_back_to_cwd(repo, monkeypatch):
    monkeypatch.chdir(repo)
    result = mod._handler({"expected_files": _expected(["complete.md"])}, None)
    assert result == {"complete": True, "missing": [], "short": []}


def test_handler_rejects_non_list_expected_files(repo):
    common_dir = repo / ".git"
    with pytest.raises(mod.ScoutInventoryParamsError, match="expected_files"):
        mod._handler({"expected_files": "not-a-list"}, common_dir)


def test_handler_rejects_non_string_entries(repo):
    common_dir = repo / ".git"
    with pytest.raises(mod.ScoutInventoryParamsError, match="expected_files"):
        mod._handler({"expected_files": ["ok.md", 3]}, common_dir)


def test_handler_rejects_non_int_min_lines(repo):
    common_dir = repo / ".git"
    with pytest.raises(mod.ScoutInventoryParamsError, match="min_lines"):
        mod._handler(
            {"expected_files": [], "min_lines": "30"}, common_dir
        )


def test_handler_rejects_bool_min_lines(repo):
    common_dir = repo / ".git"
    with pytest.raises(mod.ScoutInventoryParamsError, match="min_lines"):
        mod._handler({"expected_files": [], "min_lines": True}, common_dir)


def test_handler_repo_root_mismatch_raises(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init")
    common_dir = repo / ".git"
    with pytest.raises(mod.ScoutInventoryParamsError, match="repo_root-mismatch"):
        mod._handler(
            {"expected_files": [], "repo_root": str(other)}, common_dir
        )
