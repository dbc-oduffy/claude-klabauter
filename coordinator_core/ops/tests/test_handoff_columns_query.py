
from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_columns_query import _handler
from coordinator_core.ops.emit.sections import handoff_columns as handoff_columns_mod
from coordinator_core.win_portability import no_console_creationflags

# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_OP_NAME = "handoff.columns"

assert len(_REGISTRY) > 0, (
    "registry is empty after 'import coordinator_core.ops' — "
    "all @register_op decorators must have fired at module import time"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_columns_query @register_op did not fire"
)


def _make_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(root), capture_output=True, check=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "user.email", "handoff-columns-query-test@claude-klabauter.test"],
        cwd=str(root), capture_output=True, check=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "user.name", "Handoff Columns Query Test"],
        cwd=str(root), capture_output=True, check=True,
        **no_console_creationflags(),
    )
    return (root / ".git").resolve()


def _write_md(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture()
def tmp_repo(tmp_path: Path):
    worktree = tmp_path / "repo"
    git_dir = _make_git_repo(worktree)

    _write_md(
        worktree / "state" / "handoffs" / "hoff-live.md",
        dedent("""\
            ---
            status: open
            deployment_state: awaiting_gate
            kind: session-handoff
            ---
            Live handoff body.
        """),
    )

    _write_md(
        worktree / "archive" / "handoffs" / "2026-08" / "hoff-archived-shipped.md",
        dedent("""\
            ---
            status: claimed
            deployment_state: shipped  # crash reconstruction clears t
            shipped_in: 99c30e8  # code ops (C0b/C1/C2/C5)
            ---
            Archived shipped handoff body.
        """),
    )

    return git_dir, worktree


class TestRowShape:
    def test_row_carries_exactly_six_keys(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={"archive": False}, repo_root=git_dir)
        records = result["records"]
        assert len(records) == 1
        row = records[0]
        assert set(row.keys()) == {
            "path", "status", "deployment_state", "predecessor", "shipped_in",
            "baton_class",
        }


class TestBatonClassDerivation:
    def test_known_kind_resolves_to_its_class(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={"archive": False}, repo_root=git_dir)
        row = result["records"][0]
        assert row["path"].endswith("hoff-live.md")
        assert row["baton_class"] == "continuation"

    def test_absent_kind_yields_none_not_a_raise(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={"archive": True}, repo_root=git_dir)
        matching = [
            row for row in result["records"] if "hoff-archived-shipped" in row["path"]
        ][0]
        assert matching["baton_class"] is None


class TestArchiveCoverageOptOut:
    def test_archived_shipped_record_appears_by_default(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={}, repo_root=git_dir)
        paths = {row["path"] for row in result["records"]}
        assert any("hoff-archived-shipped" in p for p in paths)
        matching = [
            row for row in result["records"] if "hoff-archived-shipped" in row["path"]
        ][0]
        assert matching["deployment_state"] == "shipped"

    def test_archived_shipped_record_absent_with_archive_false(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={"archive": False}, repo_root=git_dir)
        paths = {row["path"] for row in result["records"]}
        assert not any("hoff-archived-shipped" in p for p in paths)


class TestCommentContaminationCleanValues:
    def test_trailing_comment_stripped_from_deployment_state_and_shipped_in(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={"archive": True}, repo_root=git_dir)
        matching = [
            row for row in result["records"] if "hoff-archived-shipped" in row["path"]
        ][0]

        assert matching["deployment_state"] == "shipped"
        assert "#" not in str(matching["deployment_state"])
        assert "crash" not in str(matching["deployment_state"])

        shipped_in = matching["shipped_in"]
        if shipped_in is not None:
            assert shipped_in["sha"] == "99c30e8"
            assert "#" not in shipped_in["sha"]
            assert "code ops" not in shipped_in["sha"]


class TestGitLogSpawnBudget:
    def test_multi_record_query_spawns_git_log_at_most_once(self, tmp_repo, monkeypatch):
        git_dir, worktree = tmp_repo

        _write_md(
            worktree / "archive" / "handoffs" / "2026-08" / "hoff-archived-shipped-2.md",
            dedent("""\
                ---
                status: claimed
                deployment_state: shipped
                shipped_in: abc1234
                ---
                Second archived shipped handoff body.
            """),
        )

        real_run = subprocess.run
        call_count = {"n": 0}

        def _counting_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) > 2 and cmd[0] == "git" and "log" in cmd:
                call_count["n"] += 1
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(handoff_columns_mod.subprocess, "run", _counting_run)

        result = _handler(params={"archive": True}, repo_root=git_dir)

        assert len(result["records"]) == 3
        assert call_count["n"] <= 1, (
            f"expected at most 1 'git log' spawn for the whole batch, got {call_count['n']}"
        )
