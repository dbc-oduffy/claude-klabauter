"""Characterization tests for coordinator_core.ops.probe_fresh_repo_noop.

Exercises the three-axis freshness check (probe_fresh_repo) directly against
throwaway directory trees under tmp_path, and the registered JSON-RPC handler
(_probe_fresh_repo_noop) with a fake repo_root derived under tmp_path — never
against claude-klabauter's own working tree, and never via a mutating git
command (this op does not invoke git at all; only pathlib checks).

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
(wave 1, chunk w2-fresh-repo-noop)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from coordinator_core.ops.probe_fresh_repo_noop import (
    _probe_fresh_repo_noop,
    probe_fresh_repo,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# probe_fresh_repo — three-axis check
# ---------------------------------------------------------------------------


def test_fresh_repo_all_three_axes_met(tmp_path: Path) -> None:
    """A brand-new scaffold (no DIRECTORY.md, no archive/, no tasks/) is fresh."""
    is_fresh, reasons = probe_fresh_repo(tmp_path)

    assert is_fresh is True
    assert len(reasons) == 3
    assert any("DIRECTORY.md" in r for r in reasons)
    assert any("archive/completed" in r for r in reasons)
    assert any("tasks/" in r for r in reasons)


def test_directory_md_present_is_not_fresh(tmp_path: Path) -> None:
    (tmp_path / "DIRECTORY.md").write_text("# Directory\n", encoding="utf-8")

    is_fresh, reasons = probe_fresh_repo(tmp_path)

    assert is_fresh is False
    assert any("DIRECTORY.md exists" in r for r in reasons)


def test_nonempty_archive_completed_is_not_fresh(tmp_path: Path) -> None:
    completed = tmp_path / "archive" / "completed"
    completed.mkdir(parents=True)
    (completed / "2026-01-01-thing.md").write_text("done", encoding="utf-8")

    is_fresh, reasons = probe_fresh_repo(tmp_path)

    assert is_fresh is False
    assert any("non-empty" in r for r in reasons)


def test_empty_archive_completed_still_fresh(tmp_path: Path) -> None:
    completed = tmp_path / "archive" / "completed"
    completed.mkdir(parents=True)

    is_fresh, reasons = probe_fresh_repo(tmp_path)

    assert is_fresh is True
    assert any("archive/completed/ is empty" in r for r in reasons)


def test_tasks_with_distillable_md_is_not_fresh(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "todo.md").write_text("- [ ] thing\n", encoding="utf-8")

    is_fresh, reasons = probe_fresh_repo(tmp_path)

    assert is_fresh is False
    assert any("distillable" in r for r in reasons)


def test_tasks_dir_without_md_files_still_fresh(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "some-uuid-dir").mkdir()
    (tasks_dir / "some-uuid-dir" / "notes.txt").write_text("x", encoding="utf-8")

    is_fresh, reasons = probe_fresh_repo(tmp_path)

    assert is_fresh is True


def test_all_three_axes_unmet(tmp_path: Path) -> None:
    (tmp_path / "DIRECTORY.md").write_text("# D\n", encoding="utf-8")
    completed = tmp_path / "archive" / "completed"
    completed.mkdir(parents=True)
    (completed / "x.md").write_text("x", encoding="utf-8")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "todo.md").write_text("x", encoding="utf-8")

    is_fresh, reasons = probe_fresh_repo(tmp_path)

    assert is_fresh is False
    assert len(reasons) == 3


# ---------------------------------------------------------------------------
# _probe_fresh_repo_noop — registered handler
# ---------------------------------------------------------------------------


def test_handler_fresh_repo_returns_contract_shape(tmp_path: Path) -> None:
    repo = tmp_path / "fresh-repo"
    repo.mkdir()
    common_dir = repo / ".git"
    common_dir.mkdir()

    result = _run(_probe_fresh_repo_noop({}, repo_root=common_dir))

    assert result == {
        "is_fresh": True,
        "reasons": [
            "no DIRECTORY.md at repo root",
            "archive/completed/ absent",
            "tasks/ absent",
        ],
    }


def test_handler_populated_repo_returns_not_fresh(tmp_path: Path) -> None:
    repo = tmp_path / "populated-repo"
    common_dir = repo / ".git"
    common_dir.mkdir(parents=True)
    (repo / "DIRECTORY.md").write_text("# Directory\n", encoding="utf-8")

    result = _run(_probe_fresh_repo_noop({}, repo_root=common_dir))

    assert result["is_fresh"] is False


def test_handler_none_repo_root_raises() -> None:
    try:
        _run(_probe_fresh_repo_noop({}, repo_root=None))
    except ValueError as exc:
        assert "_origin_worktree" in str(exc)
    else:
        raise AssertionError("expected ValueError for repo_root=None")


def test_handler_double_invocation_is_idempotent(tmp_path: Path) -> None:
    """AC7 — a second invocation with identical inputs against an unchanged tree
    is a safe no-op that returns the identical result (no mutation occurs at all,
    so this also proves the check performs no side effects)."""
    repo = tmp_path / "repo"
    common_dir = repo / ".git"
    common_dir.mkdir(parents=True)
    tasks_dir = repo / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "todo.md").write_text("- [ ] x\n", encoding="utf-8")

    first = _run(_probe_fresh_repo_noop({}, repo_root=common_dir))
    second = _run(_probe_fresh_repo_noop({}, repo_root=common_dir))

    assert first == second
    assert first["is_fresh"] is False
