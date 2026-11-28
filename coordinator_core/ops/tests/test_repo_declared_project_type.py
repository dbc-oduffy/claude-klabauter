"""coordinator_core/ops/tests/test_repo_declared_project_type.py -- the "repo.declared_project_type" op."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import repo_declared_project_type as mod
from coordinator_core.ue_project_detection import declared_ue


def _repo(tmp_path: Path, body: str | None, rel: str = "coordinator.local.md") -> Path:
    top = tmp_path / "repo"
    (top / ".git").mkdir(parents=True)
    if body is not None:
        target = top / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return top


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("repo.declared_project_type spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)


def test_unreal_fixture(tmp_path: Path) -> None:
    top = _repo(tmp_path, "---\nproject_type: game-dev\nproject_subtypes: [unreal]\n---\n")
    out = mod.handle({"root": str(top)})
    assert out["project_type"] == "game-dev"
    assert out["project_subtypes"] == ["unreal"]
    assert out["source_file"] == (top.resolve() / "coordinator.local.md").as_posix()
    assert out["ue"] is True and out["ue"] == declared_ue(top)


def test_non_unreal_fixture(tmp_path: Path) -> None:
    top = _repo(tmp_path, "---\nproject_type: web-dev\n---\n", rel=".claude/coordinator.local.md")
    out = mod.handle({"root": str(top)})
    assert (out["project_type"], out["project_subtypes"], out["ue"]) == ("web-dev", [], False)
    assert out["source_file"].endswith(".claude/coordinator.local.md")
    assert out["ue"] == declared_ue(top)


def test_undeclared_fixture(tmp_path: Path) -> None:
    for body in (None, "---\nother: 1\n---\n"):
        top = _repo(tmp_path / str(body is None), body)
        assert mod.handle({"root": str(top)}) == {
            "project_type": None,
            "project_subtypes": [],
            "source_file": None,
            "ue": None,
        }
        assert declared_ue(top) is None


def test_subdirectory_root_resolves_to_toplevel(tmp_path: Path) -> None:
    top = _repo(tmp_path, "---\nproject_type: unreal\n---\n")
    sub = top / "a" / "b"
    sub.mkdir(parents=True)
    out = mod.handle({"root": str(sub)})
    assert out["project_type"] == "unreal" and out["ue"] is declared_ue(top) is True


def test_git_file_marks_toplevel(tmp_path: Path) -> None:
    top = tmp_path / "wt"
    top.mkdir()
    (top / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (top / "coordinator.local.md").write_text("---\nproject_type: general\n---\n", encoding="utf-8")
    sub = top / "x"
    sub.mkdir()
    assert mod.handle({"root": str(sub)})["ue"] is False


@pytest.mark.parametrize("params", [{}, {"root": ""}, {"root": "relative/path"}])
def test_bad_root_refused(params) -> None:
    with pytest.raises(ValueError):
        mod.handle(params)


def test_missing_dir_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        mod.handle({"root": str(tmp_path / "nope")})
