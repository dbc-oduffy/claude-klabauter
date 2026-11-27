"""measured_rev_drift: record absent, matching, edited and re-created paths; spawn-free."""

import json
import subprocess

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_RECORD
from pathlib import Path
import pytest


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.txt").write_text("one\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return run_dir


def _record(run_dir, repo, removed=()) -> None:
    blobs = {"a.txt": commit.checkin_blob_sha(repo, "a.txt")}
    (run_dir / MEASURED_REV_RECORD).write_text(
        json.dumps({"sha": "s", "base_sha": "b", "paths": ["a.txt"], "removed": list(removed), "blobs": blobs})
    )


def test_no_record(tmp_path: Path) -> None:
    run_dir = _repo(tmp_path)
    assert mr.measured_rev_drift(run_dir, tmp_path) == []


def test_match_edit_and_recreated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _repo(tmp_path)
    _record(run_dir, tmp_path, removed=["gone.txt"])

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert mr.measured_rev_drift(run_dir, tmp_path) == []
    (tmp_path / "a.txt").write_text("two\n")
    assert mr.measured_rev_drift(run_dir, tmp_path) == ["a.txt"]
    (tmp_path / "a.txt").write_text("one\n")
    (tmp_path / "gone.txt").write_text("x")
    assert mr.measured_rev_drift(run_dir, tmp_path) == ["gone.txt"]


def _first_read_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commit, "checkin_blob_sha", lambda root, rel: None)
    monkeypatch.setattr(mr, "_reread_bounded", lambda root, rel, deadline=None: None)


def test_diagnosing_read_that_hashed_supplies_the_measured_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _repo(tmp_path)
    _record(run_dir, tmp_path)
    recorded = commit.checkin_blob_sha(tmp_path, "a.txt")
    _first_read_dropped(monkeypatch)

    monkeypatch.setattr(
        mr, "_diagnose_bounded", lambda *a, **k: commit.BlobDiagnosis(recorded, "first read stalled")
    )
    assert mr.measured_rev_drift(run_dir, tmp_path) == []

    monkeypatch.setattr(
        mr, "_diagnose_bounded", lambda *a, **k: commit.BlobDiagnosis("0" * 40, "first read stalled")
    )
    assert mr.measured_rev_drift(run_dir, tmp_path) == ["a.txt"]


def test_read_between_reread_and_remeasure_bounds_remeasures_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    run_dir = _repo(tmp_path)
    _record(run_dir, tmp_path)
    real_read = commit._checkin_blob_sha_read
    monkeypatch.setattr(commit, "checkin_blob_sha", lambda root, rel: None)

    def slow_read(root, rel):
        time.sleep(0.4)
        return real_read(root, rel)

    monkeypatch.setattr(commit, "_checkin_blob_sha_read", slow_read)
    assert 0.3 < 0.4 < mr.REMEASURE_DEADLINE_SECS
    assert mr.remeasure_reason(run_dir, tmp_path) is None


def test_deleted_recorded_path_drifts(tmp_path: Path) -> None:
    run_dir = _repo(tmp_path)
    _record(run_dir, tmp_path)
    (tmp_path / "a.txt").unlink()
    assert mr.measured_rev_drift(run_dir, tmp_path) == ["a.txt"]
