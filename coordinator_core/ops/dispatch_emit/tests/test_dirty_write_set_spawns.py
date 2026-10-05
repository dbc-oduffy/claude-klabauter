"""Real-git cases for `dirty_write_set`: CLI plan-route refusal before any file
reaches disk, and case-folded pathspecs against a real repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import dirty_write_set as tool

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# --- CLI wiring: refusal precedes any file reaching disk (real git) -----------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _real_repo_with_plan(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    plan = repo / "docs" / "plans" / "p.md"
    plan.write_text(_plan_text(), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, plan


def _plan_text() -> str:
    return (
        "---\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: Do a thing\n  writes: [\"a.txt\"]\n  body: |\n"
        "    Write a.txt.\n```\n"
    )


def test_cli_plan_route_refuses_dirty_write_set_before_writing(tmp_path, monkeypatch, capsys) -> None:
    repo, plan = _real_repo_with_plan(tmp_path)
    (repo / "a.txt").write_text("peer edit\n", encoding="utf-8")
    out = repo / "p.workflow.mjs"
    monkeypatch.chdir(repo)
    assert cli_module.main(["--plan", str(plan), "--out", str(out)]) == cli_module.EXIT_DATA_ERROR
    err = capsys.readouterr().err
    assert "a.txt" in err and "emission refused" in err
    assert not out.exists()


def test_cli_plan_route_emits_when_write_set_is_clean(tmp_path, monkeypatch) -> None:
    repo, plan = _real_repo_with_plan(tmp_path)
    out = repo / "p.workflow.mjs"
    monkeypatch.chdir(repo)
    assert cli_module.main(["--plan", str(plan), "--out", str(out)]) == cli_module.EXIT_OK
    assert out.is_file()


def test_real_git_finds_dirty_path_declared_in_other_case(tmp_path) -> None:
    repo, _plan = _real_repo_with_plan(tmp_path)
    (repo / "a.txt").write_text("peer edit\n", encoding="utf-8")
    assert tool._dirty_in_write_set(["A.TXT"], repo, ignorecase=True) == ["a.txt"]
