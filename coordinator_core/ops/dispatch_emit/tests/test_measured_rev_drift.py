"""measured_rev_drift: record absent, matching, edited and re-created paths; spawn-free."""

import json
import subprocess

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_RECORD


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.txt").write_text("one\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return run_dir


def _record(run_dir, repo, removed=()):
    blobs = {"a.txt": commit.checkin_blob_sha(repo, "a.txt")}
    (run_dir / MEASURED_REV_RECORD).write_text(
        json.dumps({"sha": "s", "base_sha": "b", "paths": ["a.txt"], "removed": list(removed), "blobs": blobs})
    )


def test_no_record(tmp_path):
    run_dir = _repo(tmp_path)
    assert mr.measured_rev_drift(run_dir, tmp_path) == []


def test_match_edit_and_recreated(tmp_path, monkeypatch):
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


def test_deleted_recorded_path_drifts(tmp_path):
    run_dir = _repo(tmp_path)
    _record(run_dir, tmp_path)
    (tmp_path / "a.txt").unlink()
    assert mr.measured_rev_drift(run_dir, tmp_path) == ["a.txt"]
