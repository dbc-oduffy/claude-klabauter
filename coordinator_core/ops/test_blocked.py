"""Characterization + parity tests for coordinator_core.ops.blocked.

Port of: blocked.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: archive/specs/2026-05-05-script-first-deterministic-ops.md §T2
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.blocked import main


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def _write_handoff(repo: Path, name: str, frontmatter: str) -> None:
    handoffs_dir = repo / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / name).write_text(f"---\n{frontmatter}\n---\nbody\n")


def test_not_a_git_repo_exits_1(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "ERROR: not inside a git repository" in captured.err


def test_empty_repo_no_blocked_items(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "== Blocked handoffs ==" in out
    assert "== Blocked todo items ==" in out
    assert "  (none)" in out
    assert "No blocked items found." in out


def test_tasks_dir_absent_reports_not_found(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "  (tasks/ not found)" in out


def test_blocked_todo_item_detected(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    todo_dir = tmp_path / "tasks" / "some-feature"
    todo_dir.mkdir(parents=True)
    (todo_dir / "todo.md").write_text(
        "status: blocked\nblocked: waiting on review\n"
    )
    monkeypatch.chdir(tmp_path)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tasks/some-feature/todo.md:" in out
    assert "1:status: blocked" in out
    assert "2:blocked: waiting on review" in out
    assert "No blocked items found." not in out


def test_paused_and_waiting_on_markers_detected(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    todo_dir = tmp_path / "tasks" / "other-feature"
    todo_dir.mkdir(parents=True)
    (todo_dir / "todo.md").write_text(
        "status: paused\nwaiting on: upstream review\n"
    )
    monkeypatch.chdir(tmp_path)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "status: paused" in out
    assert "waiting on: upstream review" in out


def test_non_matching_todo_file_ignored(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    todo_dir = tmp_path / "tasks" / "clean-feature"
    todo_dir.mkdir(parents=True)
    (todo_dir / "todo.md").write_text("status: in_progress\nnothing to see here\n")
    monkeypatch.chdir(tmp_path)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean-feature" not in out
    assert "No blocked items found." in out


def test_todo_file_depth_is_exactly_two(tmp_path, capsys, monkeypatch):
    """A todo.md nested deeper than tasks/<name>/todo.md must NOT be picked up
    (mindepth 2, maxdepth 2 in the bash oracle — this test pins that exact depth)."""
    _init_git_repo(tmp_path)
    nested = tmp_path / "tasks" / "feature" / "sub" / "todo.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("status: blocked\n")
    monkeypatch.chdir(tmp_path)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sub/todo.md" not in out
    assert "No blocked items found." in out


def test_blocked_handoff_surfaced_and_marks_found(tmp_path, capsys, monkeypatch):
    """A `state/handoffs/*.md` record with `status: blocked` is rendered via the
    native records seam (no node subprocess) and flips found_any (suppressing
    the trailing 'No blocked items found.' line)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    _write_handoff(
        repo,
        "some-handoff.md",
        "title: Some Handoff\nstatus: blocked\n",
    )
    monkeypatch.chdir(repo)

    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "- [Some Handoff](state/handoffs/some-handoff.md) — blocked" in out
    assert "No blocked items found." not in out


def test_blocked_handoff_deployment_state_preferred_over_status(tmp_path, capsys, monkeypatch):
    """Parity with query-records.js TYPE_DISPLAY.handoff (query-records.js:308):
    `deployment_state` takes priority over `status` when both are present."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    _write_handoff(
        repo,
        "paused-handoff.md",
        "title: Paused Handoff\nstatus: paused\ndeployment_state: staged\n",
    )
    monkeypatch.chdir(repo)

    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "- [Paused Handoff](state/handoffs/paused-handoff.md) — staged" in out


def test_non_blocked_handoff_not_surfaced(tmp_path, capsys, monkeypatch):
    """A handoff with a non-matching status is excluded by the `where` clause,
    leaving the handoffs section a deterministic '(none)'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    _write_handoff(
        repo,
        "open-handoff.md",
        "title: Open Handoff\nstatus: open\n",
    )
    monkeypatch.chdir(repo)

    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "== Blocked handoffs ==" in out
    assert "  (none)" in out
    assert "open-handoff.md" not in out
