"""review.prep: an unparseable undeclared .py is reported as foreign, never refused; a declared one still refuses."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BROKEN = "rung = 1 if rung not in RUNGS:\n    pass\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "own.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "foreign.py").write_text("y = 1\n", encoding="utf-8")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _call(repo: Path, paths: list[str], undeclared: list[str]) -> dict:
    base = _git(repo, "rev-parse", "HEAD").strip()
    return rp.review_prep(
        repo, base=base, paths=paths, slice_prefix="prep-f", plan_path="docs/plans/p.md",
        session_id="sess-own", undeclared_edits=undeclared,
    )


def test_unparseable_undeclared_edit_is_listed_and_own_paths_still_sliced(repo):
    (repo / "own.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "foreign.py").write_text(BROKEN, encoding="utf-8")
    reply = _call(repo, ["own.py", "foreign.py"], ["foreign.py"])
    assert not reply["error"]
    assert [f["path"] for f in reply["foreign_unparseable"]] == ["foreign.py"]
    sliced = " ".join(str(s) for s in reply["slices"])
    assert "own.py" in sliced and "foreign.py" not in sliced


def test_unparseable_declared_path_still_refuses(repo):
    (repo / "own.py").write_text(BROKEN, encoding="utf-8")
    reply = _call(repo, ["own.py"], [])
    assert "py-unparseable: own.py" in reply["error"]


def test_declared_unparseable_refuses_even_beside_a_foreign_one(repo):
    (repo / "own.py").write_text(BROKEN, encoding="utf-8")
    (repo / "foreign.py").write_text(BROKEN, encoding="utf-8")
    reply = _call(repo, ["own.py", "foreign.py"], ["foreign.py"])
    assert "own.py" in reply["error"] and "foreign.py" not in reply["error"]


def test_only_foreign_unparseable_returns_empty_review_not_refusal(repo):
    (repo / "foreign.py").write_text(BROKEN, encoding="utf-8")
    reply = _call(repo, ["foreign.py"], ["foreign.py"])
    assert not reply["error"] and reply["slices"] == []
    assert [f["path"] for f in reply["foreign_unparseable"]] == ["foreign.py"]
