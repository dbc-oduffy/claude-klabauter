"""The scratch exemption of the EM write budget is measured below the enclosing repo root."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import _em_write_budget as leaf


@pytest.fixture(autouse=True)
def _tmp_elsewhere(monkeypatch, tmp_path):
    # tmp_path lives under the system temp dir, which is itself exempt; point the temp dir elsewhere.
    other = tmp_path / "_systmp"
    other.mkdir()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(other))


def _repo(base):
    repo = base / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_repo_scratch_is_exempt(tmp_path):
    repo = _repo(tmp_path)
    assert leaf._is_scratch_or_tmp(str(repo / "scratch" / "x"))
    assert leaf._is_scratch_or_tmp(str(repo / "src" / "scratch" / "x.py"))


def test_scratch_above_repo_root_is_not_exempt(tmp_path):
    repo = _repo(tmp_path / "scratch" / "fam-1a")
    assert not leaf._is_scratch_or_tmp(str(repo / "src" / "x.py"))


def test_scratch_above_repo_root_still_exempts_repo_scratch(tmp_path):
    repo = _repo(tmp_path / "scratch" / "fam-1a")
    assert leaf._is_scratch_or_tmp(str(repo / "scratch" / "x"))


def test_no_repo_falls_back_to_absolute_path(tmp_path):
    assert leaf._is_scratch_or_tmp(str(tmp_path / "scratch" / "x"))
    assert not leaf._is_scratch_or_tmp(str(tmp_path / "other" / "x"))


def test_tmp_exemption_unchanged(tmp_path, monkeypatch):
    sys_tmp = tmp_path / "_systmp"
    repo = _repo(sys_tmp)
    assert leaf._is_scratch_or_tmp(str(repo / "src" / "x.py"))


def test_no_process_spawn(tmp_path, monkeypatch):
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    repo = _repo(tmp_path / "scratch")
    assert not leaf._is_scratch_or_tmp(str(repo / "src" / "x.py"))
