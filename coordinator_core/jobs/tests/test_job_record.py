"""Tests for coordinator_core.jobs.job_record: round-trip, unsafe ids, unsafe staging paths."""

from pathlib import Path

import pytest

from coordinator_core.jobs import job_record as jr


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "settings-home"
    h.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(h))
    return h


def _rec(job_id="job-1"):
    return jr.JobRecord(
        job_id=job_id, root_pid=123, root_ctime=1.5, lock_path="/x/lock",
        staging_path="/x/stage", owner_repo="project-rag", started_at="2026-11-17T00:00:00Z",
        command="index",
    )


def test_round_trip_and_path(home):
    rec = _rec()
    path = jr.write_record(rec)
    assert path == home / "jobs" / "job-1.json"
    assert jr.read_record("job-1") == rec
    assert not list((home / "jobs").glob("*.tmp"))


def test_schema_field_in_file(home):
    import json
    jr.write_record(_rec())
    assert json.loads((home / "jobs" / "job-1.json").read_text())["schema"] == "job-record/1"


def test_read_absent_is_none_and_delete_reports(home):
    assert jr.read_record("nope") is None
    jr.write_record(_rec())
    assert jr.delete_record("job-1") is True
    assert jr.delete_record("job-1") is False
    assert jr.read_record("job-1") is None


def test_null_lock_and_staging_round_trip(home):
    rec = jr.JobRecord("j", 1, 2.0, None, None, "r", "t")
    jr.write_record(rec)
    assert jr.read_record("j") == rec


@pytest.mark.parametrize("bad", ["", "..", ".", "a/b", "a\\b", "../x", "a b", "a\x00b"])
def test_unsafe_job_ids_refused(home, bad):
    with pytest.raises(jr.JobRecordError):
        jr.record_path(bad)
    with pytest.raises(jr.JobRecordError):
        jr.write_record(_rec(bad))
    with pytest.raises(jr.JobRecordError):
        jr.read_record(bad)
    with pytest.raises(jr.JobRecordError):
        jr.delete_record(bad)
    assert not (home / "jobs").exists()


def test_malformed_record_raises(home):
    (home / "jobs").mkdir()
    (home / "jobs" / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(jr.JobRecordError):
        jr.read_record("bad")
    (home / "jobs" / "old.json").write_text('{"schema": "job-record/0"}', encoding="utf-8")
    with pytest.raises(jr.JobRecordError):
        jr.read_record("old")


def test_staging_safe_path_accepted(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    stage = tmp_path / "scratch" / "stage"
    assert jr.staging_is_removable(str(stage), repo) is None


def test_staging_refusals(home, tmp_path, monkeypatch):
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    root = Path(tmp_path.anchor)
    assert "root" in jr.staging_is_removable(str(root), repo)
    assert "home" in jr.staging_is_removable(str(fake_home), repo)
    assert "settings" in jr.staging_is_removable(str(home), repo)
    assert "repo" in jr.staging_is_removable(str(repo), repo)
    assert "repo" in jr.staging_is_removable(str(repo.parent), repo)
    assert "absolute" in jr.staging_is_removable("relative/stage", repo)
    assert "absolute" in jr.staging_is_removable("", repo)


def test_staging_inside_repo_allowed(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert jr.staging_is_removable(str(repo / "scratch" / "s"), repo) is None


def test_no_subprocess_import():
    src = Path(jr.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in src and "from subprocess" not in src
