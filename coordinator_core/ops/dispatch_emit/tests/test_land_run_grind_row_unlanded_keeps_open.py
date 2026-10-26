"""A grind row with a path dropped from the landing is not reported committed and keeps its ledger."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run_grind
from coordinator_core.testing.git_write import write_seen_by_git
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "20261015T000000Z"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    for name in ("a.py", "b.py", "gone.py"):
        (root / name).write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    for name in ("a.py", "b.py"):
        write_seen_by_git(root / name, "x = 2\n")
    return root


def _call(root: Path, rows) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, "grind": {"profile": "bug", "rows": rows}}
    return land_run_grind.land(root, root / ".git", params)


def _row(row_id, paths):
    return {"row_id": row_id, "outcome": "fixed", "paths": list(paths), "deleted_paths": []}


def test_row_with_dropped_path_is_not_reported_committed(repo):
    (repo / "gone.py").unlink()
    out = _call(repo, [_row("r1", ["a.py", "gone.py"]), _row("r2", ["b.py"])])
    assert out["committed"] is True
    assert [r["row_id"] for r in out["rows"]] == ["r2"]
    assert [u["row_id"] for u in out["unlanded"]] == ["r1"]
    body = _git(["log", "-1", "--format=%B"], repo)
    assert "Grind-Row: r2 fixed" in body
    assert "Grind-Row: r1" not in body
    assert "Row-Unlanded: r1 gone.py" in body
    assert set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split()) == {"b.py"}


def test_fully_landed_rows_carry_no_unlanded_entry(repo):
    out = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])])
    assert [r["row_id"] for r in out["rows"]] == ["r1", "r2"]
    assert "Row-Unlanded" not in _git(["log", "-1", "--format=%B"], repo)
