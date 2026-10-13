"""An `--only-incomplete` re-emit carries a re-dispatched row's sole-declared dirty writes as its
own prior draft; a plain re-emit, or foreign dirt, still refuses with DirtyWriteSet."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli, dirty_write_set
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = "docs/plans/p.md"

_REAL_GUARD = dirty_write_set.guard_against_dirty_write_set


@pytest.fixture(autouse=True)
def _real_guard(monkeypatch):
    """The suite-wide conftest stubs the guard; this module exercises the real one."""
    monkeypatch.setattr(dirty_write_set, "guard_against_dirty_write_set", _REAL_GUARD)


def _plan() -> str:
    out = ""
    for rid in ("C1", "C2", "C3"):
        out += (
            f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n"
            f"  writes:\n    - docs/x/{rid}.md\n  queue_scope: project\n  disposition: open\n"
            f"  body: |\n    Do {rid}.\n"
        )
    return (
        '---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n'
        f"## Tasks\n\n```yaml plan-tasks\n{out}```\n"
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags()
    )


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.example")
    _git(r, "config", "user.name", "t")
    (r / "docs" / "plans").mkdir(parents=True)
    (r / "docs" / "x").mkdir(parents=True)
    (r / _PLAN).write_text(_plan(), encoding="utf-8")
    for rid in ("C1", "C2", "C3"):
        (r / "docs" / "x" / f"{rid}.md").write_text("v1\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    monkeypatch.chdir(r)
    return r


def _run_text(tmp_path: Path, body: str) -> str:
    p = tmp_path / "run.txt"
    p.write_text(body, encoding="utf-8")
    return str(p)


def _emit(repo: Path, tmp_path: Path, *extra: str) -> int:
    return cli.main(["--plan", str(repo / _PLAN), "--out", str(repo / "p.workflow.mjs"), *extra])


def test_only_incomplete_admits_the_partial_rows_own_leftover(repo, tmp_path, capsys):
    (repo / "docs" / "x" / "C2.md").write_text("draft\n", encoding="utf-8")
    run_text = _run_text(tmp_path, "abc1234 checkpoint(wave 1): 1 rows — C1\n")

    code = _emit(repo, tmp_path, "--only-incomplete", run_text)

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK, captured.err
    assert "C2: 1 uncommitted path(s) in its own writes carried as its prior draft" in captured.err
    assert "regenerable_dirty" not in json.loads(captured.out)


def test_same_tree_without_only_incomplete_still_refuses(repo, tmp_path, capsys):
    (repo / "docs" / "x" / "C2.md").write_text("draft\n", encoding="utf-8")

    code = _emit(repo, tmp_path)

    assert code == cli.EXIT_DATA_ERROR
    assert "docs/x/C2.md" in capsys.readouterr().err


def test_only_incomplete_leaves_a_landed_rows_dirt_outside_the_write_set(repo, tmp_path, capsys):
    (repo / "docs" / "x" / "C1.md").write_text("stray\n", encoding="utf-8")
    run_text = _run_text(tmp_path, "abc1234 checkpoint(wave 1): 1 rows — C1\n")
    (repo / "docs" / "x" / "C3.md").write_text("draft\n", encoding="utf-8")
    code = _emit(repo, tmp_path, "--only-incomplete", run_text)

    assert code == cli.EXIT_OK, capsys.readouterr().err


def test_trailer_landed_row_with_dirty_sole_path_is_not_dispatched(repo, tmp_path, capsys):
    (repo / "docs" / "x" / "C3.md").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo, "commit", "-q", "-m", "checkpoint(wave 1): 1 rows — C3",
        "-m", f"Checkpoint-Plan: {_PLAN}\nCheckpoint-Base: " + "a" * 40,
    )
    (repo / "docs" / "x" / "C3.md").write_text("dirty after landing\n", encoding="utf-8")
    run_text = _run_text(tmp_path, "nothing landed in the run text\n")

    code = _emit(repo, tmp_path, "--only-incomplete", run_text)

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK, captured.err
    script = (repo / "p.workflow.mjs").read_text(encoding="utf-8")
    assert "_rows['C3'] = _runRow(" not in script
    assert "_rows['C2'] = _runRow(" in script
