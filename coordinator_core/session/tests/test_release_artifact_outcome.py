"""release_artifact_outcome tells released, already-absent and held-by-another apart."""

from __future__ import annotations

import pytest

from coordinator_core.session import claims, liveness


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _claim(repo, class_="handoff", name="b.md"):
    d = repo / ".git" / "coordinator-sessions" / f"{class_}-claims" / name
    d.mkdir(parents=True)
    return d


def test_released_removes_claim(repo, monkeypatch):
    d = _claim(repo)
    monkeypatch.setattr(liveness, "claim_held_by_me", lambda *a, **k: True)
    assert claims.release_artifact_outcome("handoff", "b.md", str(repo), my_sid="s") == claims.RELEASE_RELEASED
    assert not d.exists()
    assert claims.release_artifact("handoff", "b.md", str(repo), my_sid="s") is True


def test_absent_claim_dir(repo):
    assert claims.release_artifact_outcome("handoff", "b.md", str(repo), my_sid="s") == claims.RELEASE_ALREADY_ABSENT


def test_bad_baton_root(tmp_path):
    assert claims.release_artifact_outcome("handoff", "b.md", str(tmp_path), my_sid="s") == claims.RELEASE_ALREADY_ABSENT


@pytest.mark.parametrize("held_on_read", [1, 2])
def test_held_by_another_keeps_claim(repo, monkeypatch, held_on_read):
    d = _claim(repo)
    n = {"i": 0}

    def fake(*a, **k):
        n["i"] += 1
        return n["i"] != held_on_read

    monkeypatch.setattr(liveness, "claim_held_by_me", fake)
    assert claims.release_artifact_outcome("handoff", "b.md", str(repo), my_sid="s") == claims.RELEASE_HELD_BY_ANOTHER
    assert d.exists()
    assert claims.release_artifact("handoff", "b.md", str(repo), my_sid="s") is True


def test_invalid_class_boolean_seam_false(repo, capsys):
    assert claims.release_artifact_outcome("bogus", "b.md", str(repo)) == claims.RELEASE_INVALID_CLASS
    assert "invalid class 'bogus'" in capsys.readouterr().err
    assert claims.release_artifact("bogus", "b.md", str(repo)) is False
