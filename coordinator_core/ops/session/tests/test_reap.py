"""
Tests for coordinator_core.ops.session.reap._prune_stale_agent_archive (sub-reap
(iv)): the retention prune for .archive/_agents-* entries left behind by
sub-reap (ii), which moves stale agent dirs into .archive/ but never deletes
them (the unbounded-growth leak this sub-reap closes).

Spec backlink: dispatch brief "Close an unbounded-growth leak that is
unambiguously this repo's own" (2026-08-14).

Negative-spec: does not exercise sub-reaps (i)/(ii)/(iii) or the session.reap
op handler's cadence-gate/liveness plumbing — pure unit coverage of the new
sync helper against a tmp_path tree, matching sibling test files' fixture
idiom (no real .git/, no daemon spawn).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.session import reap
from coordinator_core.session import core, liveness as session_liveness

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
    """An _agents-* archive entry older than the 14d retention window is
    deleted; one within the window is kept."""
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
    """A stray non-`_agents-*` archive entry (e.g. a sub-reap (i) stale-session
    archive) is never globbed or deleted — confined strictly to the _agents-*
    prefix."""
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
    """No .archive/ directory at all — returns empty, no error."""
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir(parents=True)

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    assert pruned == []
    assert failed == []


def test_stat_failure_is_fail_open_skip_not_raise(tmp_path, monkeypatch):
    """An OSError raised while stat'ing one entry is logged and skipped —
    never aborts the sweep and never raises into the caller."""
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

    monkeypatch.undo()  # restore real Path.stat before the assertion's own .exists() call

    assert pruned == []
    assert len(failed) == 1
    assert failed[0]["id"] == "_agents-broken-20260101"
    assert entry.exists()  # never removed — fail-open keeps the entry


def test_rmtree_failure_is_fail_open_skip_not_raise(tmp_path, monkeypatch):
    """An OSError raised by shutil.rmtree for one entry is logged and
    skipped — never aborts the sweep and never raises into the caller."""
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
    assert broken_entry.exists()  # never removed — fail-open keeps the entry
    assert not ok_entry.exists()


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _sdir(repo, sid):
    return Path(core.sessions_dir(cwd=str(repo))) / sid


def _make_stale(sid_dir: Path):
    """Push a session dir's last_activity and EVERY on-disk record outside the
    abandonment window (>= 2 independently-stale-signal floor) AND outside
    reap's own 24h staleness threshold.

    Ages the whole directory, not just `touched.txt`: `session_abandoned`'s
    freshest-signal gate reads `newest_record_mtime(sdir)`, which was widened
    off any single filename literal, so `core.init`'s one-shot creation stamps
    (`head_at_start`, `started_at`) count as records too. Back-dating
    `touched.txt` alone leaves them at creation-time-fresh, the gate reads
    NOT-abandoned, and the session is never reaped — the fixture, not the
    reaper, being wrong."""
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


# Sub-reap (i) selects candidates by a positive uuid-shape test
# (docs/plans/2026-08-26-the-reaper-identifies-sessions-positively.md, C2), so a
# fixture sid MUST be uuid-shaped or `_reap_stale_sessions` never considers it at
# all. These four ids carry the old mnemonic names in their comments; the shape,
# not the wording, is what the reaper reads. A non-uuid sid here does not fail
# loudly — it makes the test vacuous, which is why they are named constants.
_SID_ABANDONED_LIVE = "aaaaaaaa-1111-4111-8111-000000000001"  # was "abandoned-live"
_SID_LIVE_QUIET = "bbbbbbbb-2222-4222-8222-000000000002"  # was "live-quiet"
_SID_DEAD = "cccccccc-3333-4333-8333-000000000003"  # was "dead-sid"
_SID_UNKNOWN_TS = "dddddddd-4444-4444-8444-000000000004"  # was "unknown-ts"


class TestReapStaleSessionsLiveWitnessAbandonment:
    """C5 — first test of `_reap_stale_sessions` (grep-confirmed: no prior
    test referenced it by name). Mirrors C4's `compute_scope` fixture idiom:
    a live-witness session (`sid in live_sids`) is only let past the
    liveness skip when it is independently abandoned per the real, unmocked
    `session.liveness.session_abandoned` — never on a doctored
    `last_activity` alone."""

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
        """The ordinary live case is untouched: a live-witness session with
        a fresh touched.txt (so `session_abandoned` reads NOT-abandoned per
        its freshest-signal gate) is kept even though `last_activity` alone
        is stale — the liveness skip must not be removed, only widened for
        the abandoned case."""
        repo = _make_repo(tmp_path)
        core.init(_SID_LIVE_QUIET, cwd=str(repo))
        sessions_dir = Path(core.sessions_dir(cwd=str(repo)))
        sdir = _sdir(repo, _SID_LIVE_QUIET)
        core.update_meta_field(str(sdir), "last_activity", "2000-01-01T00:00:00Z")
        (sdir / "touched.txt").write_text("x", encoding="utf-8")  # fresh mtime

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
        """Sanity baseline, unaffected by C5: a sid NOT in live_sids at all
        still reaps on the pre-existing 24h path, independent of
        abandonment."""
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
