"""A resume row set stages only those rows and admits the halted run's own uncommitted leftovers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import plan_route
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN_REL = "docs/plans/p.md"


def _row(rid: str, writes: list[str], extra: str = "", dep: str = "") -> str:
    w = "".join(f"    - {p}\n" for p in writes)
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: {writes[0]}\n"
        f"  writes:\n{w}{extra}{dep}"
    )


def _plan(rows: str) -> str:
    return f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n"


_DEP_C1 = "  depends_on:\n    - chunk: C1\n      gate_kind: output-consumption-runtime\n"


@pytest.fixture(autouse=True)
def _real_guard_and_env(monkeypatch):
    """The suite conftest stubs the write-set guard; these tests run the real one."""
    from coordinator_core.ops.dispatch_emit import dirty_write_set

    monkeypatch.setattr(dirty_write_set, "guard_against_dirty_write_set", _REAL_GUARD)
    monkeypatch.setenv("COORDINATOR_WORKFLOW_ADMISSION_DISABLE", "1")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-4333-8444-555555555555")
    from coordinator_core import machine_resolver

    real = machine_resolver.registry_get
    monkeypatch.setattr(
        machine_resolver, "registry_get", lambda key: None if key == plan_route.BOX_TERMS_KEY else real(key)
    )


from coordinator_core.ops.dispatch_emit import dirty_write_set as _dws  # noqa: E402

_REAL_GUARD = _dws.guard_against_dirty_write_set


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, **no_console_creationflags()
    )


def _git_repo(tmp_path: Path, rows: str, files: list[str]) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / PLAN_REL).write_text(_plan(rows), encoding="utf-8", newline="\n")
    for f in files:
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text("v1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _emit(repo: Path, *extra: str) -> int:
    return plan_route.main(
        ["--plan", str(repo / PLAN_REL), "--out", str(repo / "out" / "p.workflow.mjs"),
         "--repo-root", str(repo), *extra]
    )


def _stage(tmp_path: Path, rows: str, only_rows: list[str]) -> dict:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_plan(rows), encoding="utf-8", newline="\n")
    return read_stage_reply(
        tmp_path, _handler({"run_id": "r1", "plan_path": PLAN_REL, "only_rows": only_rows}, repo_root=tmp_path)
    )


def test_resume_stages_only_incomplete_rows(tmp_path):
    rows = _row("C1", ["docs/x/c1.md"]) + _row("C2", ["docs/x/c2.md"], dep=_DEP_C1) + _row("C3", ["docs/x/c3.md"])
    reply = _stage(tmp_path, rows, ["C2", "C3"])
    assert "error" not in reply
    assert sorted(r["id"] for r in reply["rows"]) == ["C2", "C3"]
    assert {r["wave"] for r in reply["rows"]} == {0}
    assert not (tmp_path / reply["run_dir"] / "briefs" / "C1.md").exists()


def test_resume_admits_its_own_leftover_and_refuses_foreign_dirt(tmp_path, capsys):
    rows = (
        _row("C1", ["docs/x/c1.md"])
        + _row("C2", ["docs/x/c2.md"])
        + _row("C3", ["docs/x/shared.md"])
        + _row("C4", ["docs/x/shared.md"])
    )
    repo = _git_repo(tmp_path, rows, [f"docs/x/{n}.md" for n in ("c1", "c2", "shared")])
    (repo / "docs" / "x" / "c2.md").write_text("draft\n", encoding="utf-8")
    assert _emit(repo, "--resume-rows", "C2") == plan_route.EXIT_OK
    err = capsys.readouterr().err
    assert "C2: 1 uncommitted path(s) in its own writes carried as its prior draft" in err

    (repo / "docs" / "x" / "shared.md").write_text("dirt\n", encoding="utf-8")
    assert _emit(repo, "--resume-rows", "C3,C4") == plan_route.EXIT_DATA_ERROR
    assert "docs/x/shared.md" in capsys.readouterr().err


def test_resume_of_only_em_owned_rows_returns_them_owed(tmp_path):
    rows = (
        _row("U1", ["docs/x/u1.md"])
        + _row("M1", ["docs/x/m1.md"], extra="  performer: em\n")
        + _row("M2", ["docs/x/m2.md"], extra="  performer: em\n")
    )
    reply = _stage(tmp_path, rows, ["M1", "M2"])
    assert "error" not in reply
    assert reply["rows"] == []
    assert [o["id"] for o in reply["owed"]] == ["M1", "M2"]
