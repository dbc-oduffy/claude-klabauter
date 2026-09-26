
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.session import reap
from coordinator_core.session import core, liveness as session_liveness
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _make_archive_entry(archive_root: Path, name: str, *, age_seconds: float) -> Path:
    entry = archive_root / name
    entry.mkdir(parents=True)
    (entry / "touched.txt").write_text("x", encoding="utf-8")
    stamp = time.time() - age_seconds
    import os

    os.utime(entry, (stamp, stamp))
    return entry


def test_prunes_entries_older_than_retention_window(tmp_path):
    sessions_dir = tmp_path / "coordinator-sessions"
    archive_root = sessions_dir / ".archive"
    archive_root.mkdir(parents=True)

    old_entry = _make_archive_entry(
        archive_root, "_agents-old-20260101",
        age_seconds=reap._AGENT_ARCHIVE_RETENTION_SECONDS + 3600,
    )
    fresh_entry = _make_archive_entry(
        archive_root, "_agents-fresh-20260814",
        age_seconds=3600,
    )

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    assert pruned == ["_agents-old-20260101"]
    assert failed == []
    assert not old_entry.exists()
    assert fresh_entry.exists()


def test_never_touches_non_agents_archive_entries(tmp_path):
    sessions_dir = tmp_path / "coordinator-sessions"
    archive_root = sessions_dir / ".archive"
    archive_root.mkdir(parents=True)

    session_entry = _make_archive_entry(
        archive_root, "sess-abc-2026-01-01",
        age_seconds=reap._AGENT_ARCHIVE_RETENTION_SECONDS + 3600,
    )

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    assert pruned == []
    assert failed == []
    assert session_entry.exists()


def test_missing_archive_dir_is_a_noop(tmp_path):
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir(parents=True)

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    assert pruned == []
    assert failed == []


def test_stat_failure_is_fail_open_skip_not_raise(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "coordinator-sessions"
    archive_root = sessions_dir / ".archive"
    archive_root.mkdir(parents=True)

    entry = _make_archive_entry(
        archive_root, "_agents-broken-20260101",
        age_seconds=reap._AGENT_ARCHIVE_RETENTION_SECONDS + 3600,
    )

    real_stat = Path.stat

    def _boom_stat(self, *args, **kwargs):
        if self.name == "_agents-broken-20260101":
            raise OSError("simulated stat failure")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _boom_stat)

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    monkeypatch.undo()

    assert pruned == []
    assert len(failed) == 1
    assert failed[0]["id"] == "_agents-broken-20260101"
    assert entry.exists()


def test_rmtree_failure_is_fail_open_skip_not_raise(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "coordinator-sessions"
    archive_root = sessions_dir / ".archive"
    archive_root.mkdir(parents=True)

    broken_entry = _make_archive_entry(
        archive_root, "_agents-rmbroken-20260101",
        age_seconds=reap._AGENT_ARCHIVE_RETENTION_SECONDS + 3600,
    )
    ok_entry = _make_archive_entry(
        archive_root, "_agents-rmok-20260101",
        age_seconds=reap._AGENT_ARCHIVE_RETENTION_SECONDS + 3600,
    )

    import shutil

    _orig_rmtree = shutil.rmtree

    def _boom_rmtree(path, *args, **kwargs):
        if Path(path).name == "_agents-rmbroken-20260101":
            raise OSError("simulated rmtree failure")
        return _orig_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(reap.shutil, "rmtree", _boom_rmtree)

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    assert pruned == ["_agents-rmok-20260101"]
    assert len(failed) == 1
    assert failed[0]["id"] == "_agents-rmbroken-20260101"
    assert "rm failed" in failed[0]["reason"]
    assert broken_entry.exists()
    assert not ok_entry.exists()


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


def _sdir(repo, sid):
    return Path(core.sessions_dir(cwd=str(repo))) / sid


def _make_stale(sid_dir: Path):
    core.update_meta_field(str(sid_dir), "last_activity", "2000-01-01T00:00:00Z")
    touched = sid_dir / "touched.txt"
    stale_epoch = (
        core.now_epoch()
        - max(session_liveness._ABANDONMENT_WINDOW_SEC, reap._SESSION_STALE_SECONDS)
        - 3600
    )
    if not touched.exists():
        touched.write_text("x", encoding="utf-8")
    for record in sid_dir.iterdir():
        if record.is_file():
            os.utime(str(record), (stale_epoch, stale_epoch))


_SID_ABANDONED_LIVE = "aaaaaaaa-1111-4111-8111-000000000001"
_SID_LIVE_QUIET = "bbbbbbbb-2222-4222-8222-000000000002"
_SID_DEAD = "cccccccc-3333-4333-8333-000000000003"
_SID_UNKNOWN_TS = "dddddddd-4444-4444-8444-000000000004"


class TestReapStaleSessionsLiveWitnessAbandonment:

    def test_live_witness_abandoned_and_stale_is_reaped(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init(_SID_ABANDONED_LIVE, cwd=str(repo))
        sessions_dir = Path(core.sessions_dir(cwd=str(repo)))
        sdir = _sdir(repo, _SID_ABANDONED_LIVE)
        _make_stale(sdir)

        reaped, deferred, failed = reap._reap_stale_sessions(
            sessions_dir,
            frozenset({_SID_ABANDONED_LIVE}),
            None,
            str(repo),
        )

        assert reaped == [_SID_ABANDONED_LIVE]
        assert deferred == []
        assert failed == []
        assert not sdir.exists()

    def test_live_witness_not_abandoned_is_kept_despite_stale_last_activity(
        self, tmp_path
    ):
        repo = _make_repo(tmp_path)
        core.init(_SID_LIVE_QUIET, cwd=str(repo))
        sessions_dir = Path(core.sessions_dir(cwd=str(repo)))
        sdir = _sdir(repo, _SID_LIVE_QUIET)
        core.update_meta_field(str(sdir), "last_activity", "2000-01-01T00:00:00Z")
        (sdir / "touched.txt").write_text("x", encoding="utf-8")

        reaped, deferred, failed = reap._reap_stale_sessions(
            sessions_dir,
            frozenset({_SID_LIVE_QUIET}),
            None,
            str(repo),
        )

        assert reaped == []
        assert deferred == []
        assert failed == []
        assert sdir.exists()

    def test_dead_sid_stale_is_reaped_baseline(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init(_SID_DEAD, cwd=str(repo))
        sessions_dir = Path(core.sessions_dir(cwd=str(repo)))
        sdir = _sdir(repo, _SID_DEAD)
        core.update_meta_field(str(sdir), "last_activity", "2000-01-01T00:00:00Z")

        reaped, deferred, failed = reap._reap_stale_sessions(
            sessions_dir,
            frozenset(),
            None,
            str(repo),
        )

        assert reaped == [_SID_DEAD]
        assert deferred == []
        assert failed == []

    def test_unknown_last_activity_is_deferred_not_reaped(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init(_SID_UNKNOWN_TS, cwd=str(repo))
        sessions_dir = Path(core.sessions_dir(cwd=str(repo)))
        sdir = _sdir(repo, _SID_UNKNOWN_TS)
        core.update_meta_field(str(sdir), "last_activity", "not-a-timestamp")

        reaped, deferred, failed = reap._reap_stale_sessions(
            sessions_dir,
            frozenset(),
            None,
            str(repo),
        )

        assert reaped == []
        assert len(deferred) == 1
        assert deferred[0]["id"] == _SID_UNKNOWN_TS
        assert failed == []
        assert sdir.exists()
