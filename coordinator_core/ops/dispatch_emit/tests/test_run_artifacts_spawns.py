"""run_artifacts: per-class candidate set read from a run's own record graph, zero git."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import run as _git_run_module
from coordinator_core.ops.dispatch_emit import run_artifacts as ra

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REAL_RUN_GIT = _git_run_module.run_git

RUN = "ask-20261016T120000-bbbbbb"
OLD = "ask-20261016T100000-aaaaaa"
SIZING = "state/sizings/2026-10-16-x.yaml"
PLAN = "docs/plans/2026-10-16-x.md"
SLICE = "state/sizings/2026-10-16-x-slice-2.yaml"


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    from coordinator_core.git import run as git_run

    def boom(*a, **k):
        raise AssertionError("run_artifacts must not spawn git")

    monkeypatch.setattr(git_run, "run_git", boom)


def _w(root: Path, rel: str, text: str = "x\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


ASK = "ask-20261016T110000-dddddd"


def _git(root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(root: Path, monkeypatch) -> None:
    monkeypatch.setattr(_git_run_module, "run_git", _REAL_RUN_GIT)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")


def test_record_dir_residue_names_modified_and_untracked_but_not_clean(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)
    rec = f"state/warp/{RUN}"
    _w(tmp_path, f"{rec}/resume.json", "a")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    assert ra.record_dir_residue(tmp_path, RUN) == []
    _w(tmp_path, f"{rec}/resume.json", "b")
    _w(tmp_path, f"{rec}/prompts/sub/p.md")
    _w(tmp_path, "state/warp/other-run/x.json")
    assert ra.record_dir_residue(tmp_path, RUN) == [f"{rec}/prompts/sub/p.md", f"{rec}/resume.json"]


def test_record_dir_residue_is_empty_for_ignored_absent_or_unsafe(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch)
    (tmp_path / ".gitignore").write_text("state/warp/\n", encoding="utf-8")
    _w(tmp_path, f"state/warp/{RUN}/halted.json")
    assert ra.record_dir_residue(tmp_path, RUN) == []
    assert ra.record_dir_residue(tmp_path, "absent-run") == []
    assert ra.record_dir_residue(tmp_path, "../x") == []
