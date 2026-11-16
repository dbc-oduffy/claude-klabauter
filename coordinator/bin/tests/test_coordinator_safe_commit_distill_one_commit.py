"""`--invoking-command distill` lands a mixed product/process pathspec as one commit; others still split."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import subprocess

import pytest
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent
_PATHS = ["docs" + "/wiki/x.md", "state/y.json"]


def _load_cli_module() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    (tmp_path / "docs" / "wiki").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    (tmp_path / "docs" / "wiki" / "x.md").write_text("x", encoding="utf-8")
    (tmp_path / "state" / "y.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_INVOKING_COMMAND", raising=False)
    return tmp_path


def _run(mod, argv):
    try:
        mod.main(argv)
    except SystemExit as exc:
        return exc.code or 0
    return 0


def _new_commit_subjects(repo, seed_sha) -> list[str]:
    return [s for s in _git(repo, "log", "--format=%s", f"{seed_sha}..HEAD").splitlines() if s]


def test_distill_invoking_command_lands_one_commit_without_process_subject(repo) -> None:
    mod = _load_cli_module()
    seed = _git(repo, "rev-parse", "HEAD").strip()

    assert _run(mod, ["--invoking-command", "distill", "harvest", "--", *_PATHS]) == 0

    subjects = _new_commit_subjects(repo, seed)
    assert len(subjects) == 1
    assert "(process files)" not in subjects[0]
    assert sorted(_git(repo, "show", "--name-only", "--pretty=format:", "HEAD").split()) == sorted(_PATHS)


def test_without_distill_the_same_mixed_set_still_lands_two_commits(repo) -> None:
    mod = _load_cli_module()
    seed = _git(repo, "rev-parse", "HEAD").strip()

    assert _run(mod, ["--invoking-command", "update-docs", "harvest", "--", *_PATHS]) == 0

    subjects = _new_commit_subjects(repo, seed)
    assert len(subjects) == 2
    assert any("(process files)" in s for s in subjects)
