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


# --- through the plain `--plan` route entry point ---------------------------

ROUTE_ROWS = """\
- id: C1
  title: dispatched
  change_kind: doc-edit
  surface: docs/x/c1.md
  writes:
    - docs/x/c1.md
- id: C6
  title: held out
  change_kind: doc-edit
  surface: docs/x/c6.md
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
"""


_REAL_GUARD = tool.guard_against_dirty_write_set


def _route_repo(tmp_path: Path, monkeypatch) -> Path:
    from coordinator_core import machine_resolver
    from coordinator_core.ops.dispatch_emit import plan_route
    from coordinator_core.win_portability import no_console_creationflags

    monkeypatch.setattr(tool, "guard_against_dirty_write_set", _REAL_GUARD)
    monkeypatch.setenv("COORDINATOR_WORKFLOW_ADMISSION_DISABLE", "1")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-4333-8444-555555555555")
    real = machine_resolver.registry_get
    monkeypatch.setattr(
        machine_resolver, "registry_get", lambda key: None if key == plan_route.BOX_TERMS_KEY else real(key)
    )
    repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "p.md").write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{ROUTE_ROWS}```\n",
        encoding="utf-8", newline="\n",
    )
    for name in ("c1", "c6"):
        (repo / "docs" / "x").mkdir(exist_ok=True)
        (repo / "docs" / "x" / f"{name}.md").write_text("v1\n", encoding="utf-8")
    for args in (("init", "-q"), ("config", "user.email", "t@example.invalid"), ("config", "user.name", "t"),
                 ("add", "-A"), ("commit", "-q", "-m", "fixture")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, **no_console_creationflags())
    return repo


def _route_emit(repo: Path) -> int:
    from coordinator_core.ops.dispatch_emit import plan_route

    return plan_route.main(
        ["--plan", str(repo / "docs" / "plans" / "p.md"), "--out", str(repo / "out" / "p.workflow.mjs"),
         "--repo-root", str(repo)]
    )


# The hold is wave_map's epistemic-premise holdout (undeclared `writes`): the row survives
# `read_spine`, so only the route threading its id into the guard admits it.
def test_route_admits_dirt_of_a_row_held_out_of_the_wave(tmp_path, monkeypatch, capsys) -> None:
    repo = _route_repo(tmp_path, monkeypatch)
    (repo / "docs" / "x" / "c6.md").write_text("stranded\n", encoding="utf-8")
    assert _route_emit(repo) == 0, capsys.readouterr().err


def test_route_still_refuses_dirt_of_a_dispatched_row(tmp_path, monkeypatch, capsys) -> None:
    repo = _route_repo(tmp_path, monkeypatch)
    (repo / "docs" / "x" / "c1.md").write_text("dirt\n", encoding="utf-8")
    assert _route_emit(repo) == 1
    assert "docs/x/c1.md" in capsys.readouterr().err
