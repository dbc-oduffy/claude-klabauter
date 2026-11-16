"""release_artifact_outcome tells released, already-absent and held-by-another apart."""

from __future__ import annotations

import pytest

from coordinator_core.session import claims, liveness
from pathlib import Path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _claim(repo, class_: str = "handoff", name: str = "b.md"):
    d = repo / ".coordinator-local" / "coordinator-sessions" / f"{class_}-claims" / name
    d.mkdir(parents=True)
    return d


def test_released_removes_claim(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _claim(repo)
    monkeypatch.setattr(liveness, "claim_held_by_me", lambda *a, **k: True)
    assert claims.release_artifact_outcome("handoff", "b.md", str(repo), my_sid="s") == claims.RELEASE_RELEASED
    assert not d.exists()
    assert claims.release_artifact("handoff", "b.md", str(repo), my_sid="s") is True


def test_absent_claim_dir(repo) -> None:
    assert claims.release_artifact_outcome("handoff", "b.md", str(repo), my_sid="s") == claims.RELEASE_ALREADY_ABSENT


def test_bad_baton_root(tmp_path: Path) -> None:
    assert claims.release_artifact_outcome("handoff", "b.md", str(tmp_path), my_sid="s") == claims.RELEASE_ALREADY_ABSENT


@pytest.mark.parametrize("held_on_read", [1, 2])
def test_held_by_another_keeps_claim(repo, monkeypatch: pytest.MonkeyPatch, held_on_read) -> None:
    d = _claim(repo)
    n = {"i": 0}

    def fake(*a, **k) -> bool:
        n["i"] += 1
        return n["i"] != held_on_read

    monkeypatch.setattr(liveness, "claim_held_by_me", fake)
    assert claims.release_artifact_outcome("handoff", "b.md", str(repo), my_sid="s") == claims.RELEASE_HELD_BY_ANOTHER
    assert d.exists()
    assert claims.release_artifact("handoff", "b.md", str(repo), my_sid="s") is True


def test_invalid_class_boolean_seam_false(repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert claims.release_artifact_outcome("bogus", "b.md", str(repo)) == claims.RELEASE_INVALID_CLASS
    assert "invalid class 'bogus'" in capsys.readouterr().err
    assert claims.release_artifact("bogus", "b.md", str(repo)) is False
