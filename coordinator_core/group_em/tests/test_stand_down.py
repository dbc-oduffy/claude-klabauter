"""Tests for coordinator_core.group_em.nomination.stand_down -- per-repo and box records.

Every test points at a tmp_path record directory; the real settings home is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.group_em import nomination

_REAL_CLAIM = nomination.claim


@pytest.fixture(autouse=True)
def _human_entered(monkeypatch):
    monkeypatch.setattr(
        nomination, "claim", lambda *a, **k: _REAL_CLAIM(*a, **{"prompt_id": "p-1", **k})
    )
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})


@pytest.fixture
def repo_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return str(root.resolve())


@pytest.fixture
def record_dir(tmp_path):
    d = tmp_path / "group-em-records"
    d.mkdir()
    return d


def test_holder_stands_down_and_box_holder_clears(repo_root, record_dir):
    nomination.claim(repo_root, "sid-a", directory=record_dir)
    assert nomination.box_holder(record_dir)["session_id"] == "sid-a"

    result = nomination.stand_down(repo_root, "sid-a", record_dir)

    assert (result.ok, result.exit_code) == (True, 0)
    assert result.record["session_id"] == "sid-a"
    assert nomination.read_record(repo_root, record_dir) is None
    assert nomination.box_holder(record_dir) is None
    assert not nomination._box_record_path(record_dir).exists()


def test_non_holder_is_refused_and_both_records_kept(repo_root, record_dir):
    nomination.claim(repo_root, "sid-a", directory=record_dir)

    result = nomination.stand_down(repo_root, "sid-b", record_dir)

    assert (result.ok, result.exit_code) == (False, 5)
    assert "sid-a" in result.message
    assert nomination.read_record(repo_root, record_dir)["session_id"] == "sid-a"
    assert nomination.box_holder(record_dir)["session_id"] == "sid-a"


def test_box_record_naming_another_session_is_left_alone(repo_root, record_dir):
    nomination.claim(repo_root, "sid-a", directory=record_dir)
    other = str(Path(repo_root).parent / "other-repo")
    Path(other).mkdir()
    nomination.claim(other, "sid-b", directory=record_dir)
    assert nomination.box_holder(record_dir)["session_id"] == "sid-b"

    result = nomination.stand_down(repo_root, "sid-a", record_dir)

    assert result.ok
    assert nomination.box_holder(record_dir)["session_id"] == "sid-b"


def test_no_record_is_exit_3(repo_root, record_dir):
    result = nomination.stand_down(repo_root, "sid-a", record_dir)

    assert (result.ok, result.exit_code, result.record) == (False, 3, None)


def test_any_holder_stand_down_when_session_id_omitted(repo_root, record_dir):
    nomination.claim(repo_root, "sid-a", directory=record_dir)

    result = nomination.stand_down(repo_root, None, record_dir)

    assert (result.ok, result.exit_code) == (True, 0)
    assert nomination.box_holder(record_dir) is None
