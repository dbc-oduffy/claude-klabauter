"""Tests for coordinator_core.run_watermark: rag path (zero spawns), git fallback (one
spawn), select/cursor advance, and the stage/promote commit gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import run_watermark as rw

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = rw.git_out

    def counting(argv, **kw):
        calls.append(list(argv))
        return real(argv, **kw)

    monkeypatch.setattr(rw, "git_out", counting)
    return calls


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_git_fallback_one_spawn_counts_uncommitted_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawns) -> None:
    monkeypatch.setattr(rw, "registry_get", lambda key: None)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("1")
    (tmp_path / "docs" / "b.md").write_text("1")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "docs" / "a.md").write_text("2")
    changed = rw.changed_since(
        tmp_path, rw.Watermark("distill", base), scope_dirs=["docs"]
    )
    assert set(changed) == {"docs/a.md"}
    assert len(spawns) == 1
