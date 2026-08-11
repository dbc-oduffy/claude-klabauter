"""
coordinator_core.session.tests.test_harness_registry — C1 scoped test suite.

Spec backlink: docs/plans/2026-08-08-harness-session-registry-as-liveness-source.md § C1

All fixtures are fabricated on `tmp_path` — this suite never reads the
operator's real `~/.claude/sessions`.
"""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.session import harness_registry as hr


def _write_record(sessions_dir, filename, session_id, pid, proc_start_ticks):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    payload = {"sessionId": session_id, "pid": pid, "procStart": proc_start_ticks}
    (sessions_dir / filename).write_text(json.dumps(payload), encoding="utf-8")


def _epoch_to_filetime_ticks(epoch: float) -> int:
    return int((epoch + hr._FILETIME_EPOCH_OFFSET_SEC) * hr._FILETIME_TICKS_PER_SEC)


class TestFiletimeConversion:
    def test_known_good_pair(self):
        # 2026-08-08T00:00:00Z, verified independently: FILETIME ticks for
        # this instant computed via (epoch + 11644473600) * 1e7.
        epoch = 1783929600.0
        ticks = _epoch_to_filetime_ticks(epoch)
        assert hr._filetime_to_epoch(ticks) == pytest.approx(epoch, abs=1e-3)

    def test_out_of_band_past_rejected(self):
        epoch = time.time() - hr._SANITY_BAND_PAST_SEC - 3600
        ticks = _epoch_to_filetime_ticks(epoch)
        assert hr._filetime_to_epoch(ticks) is None

    def test_out_of_band_future_rejected(self):
        epoch = time.time() + hr._SANITY_BAND_FUTURE_SEC + 3600
        ticks = _epoch_to_filetime_ticks(epoch)
        assert hr._filetime_to_epoch(ticks) is None

    def test_non_numeric_rejected(self):
        assert hr._filetime_to_epoch("not-a-number") is None
        assert hr._filetime_to_epoch(None) is None


class TestSnapshotAndLookup:
    def test_matching_record_resolves(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        epoch = time.time() - 60
        ticks = _epoch_to_filetime_ticks(epoch)
        _write_record(sessions_dir, "12345.json", "sess-abc", 12345, ticks)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        record = hr.lookup("sess-abc")
        assert record is not None
        assert record.pid == 12345
        assert record.start_epoch == pytest.approx(epoch, abs=1e-3)

    def test_procstart_mismatch_or_out_of_band_reads_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        bad_epoch = time.time() - hr._SANITY_BAND_PAST_SEC - 3600
        bad_ticks = _epoch_to_filetime_ticks(bad_epoch)
        _write_record(sessions_dir, "1.json", "sess-bad", 1, bad_ticks)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        assert hr.lookup("sess-bad") is None
        assert hr.snapshot() == {}

    def test_unmatched_session_id_reads_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        _write_record(sessions_dir, "1.json", "sess-real", 1, ticks)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        assert hr.lookup("sess-other") is None

    def test_absent_directory_yields_empty_snapshot_and_none_lookup(self, tmp_path, monkeypatch):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(hr, "registry_dir", lambda: missing)

        assert hr.snapshot() == {}
        assert hr.lookup("anything") is None

    def test_registry_dir_none_degrades_cleanly(self, monkeypatch):
        monkeypatch.setattr(hr, "registry_dir", lambda: None)

        assert hr.snapshot() == {}
        assert hr.lookup("anything") is None

    def test_absent_file_within_present_dir_yields_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        assert hr.snapshot() == {}
        assert hr.lookup("nope") is None

    def test_malformed_json_yields_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        assert hr.snapshot() == {}
        assert hr.lookup("anything") is None

    def test_missing_procstart_yields_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        payload = {"sessionId": "sess-x", "pid": 1}
        (sessions_dir / "x.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        assert hr.snapshot() == {}
        assert hr.lookup("sess-x") is None

    def test_non_numeric_procstart_yields_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        payload = {"sessionId": "sess-y", "pid": 1, "procStart": "banana"}
        (sessions_dir / "y.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        assert hr.snapshot() == {}
        assert hr.lookup("sess-y") is None

    def test_non_numeric_pid_yields_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        payload = {"sessionId": "sess-z", "pid": "not-a-pid", "procStart": ticks}
        (sessions_dir / "z.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        assert hr.snapshot() == {}
        assert hr.lookup("sess-z") is None

    def test_float_pid_yields_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        payload = {"sessionId": "sess-float", "pid": 12345.7, "procStart": ticks}
        (sessions_dir / "float.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        assert hr.snapshot() == {}
        assert hr.lookup("sess-float") is None

    def test_non_positive_pid_yields_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        _write_record(sessions_dir, "zero.json", "sess-zero", 0, ticks)
        _write_record(sessions_dir, "neg.json", "sess-neg", -5, ticks)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        assert hr.snapshot() == {}
        assert hr.lookup("sess-zero") is None
        assert hr.lookup("sess-neg") is None


class TestExceptionBoundary:
    def test_snapshot_swallows_arbitrary_internal_exception(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "a.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        class Boom(RuntimeError):
            pass

        def _raise(self):
            raise Boom("unexpected raiser not in the enumerated list")

        monkeypatch.setattr(hr, "_parse_one", _raise)

        assert hr.snapshot() == {}

    def test_lookup_swallows_arbitrary_internal_exception(self, monkeypatch):
        class Boom(RuntimeError):
            pass

        def _raise():
            raise Boom("unexpected raiser not in the enumerated list")

        monkeypatch.setattr(hr, "snapshot", _raise)

        assert hr.lookup("anything") is None


class TestSelfRecord:
    """C1 (`docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md`)
    — the O(1) pid-keyed leg."""

    def test_self_record_hit(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        epoch = time.time() - 60
        ticks = _epoch_to_filetime_ticks(epoch)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessionId": "sess-self",
            "pid": 4242,
            "procStart": ticks,
            "cwd": str(tmp_path),
        }
        (sessions_dir / "4242.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: ((4242, 0.0), "env-hit"),
        )

        result = hr.self_record()
        assert result is not None
        session_id, record = result
        assert session_id == "sess-self"
        assert record.pid == 4242
        assert record.cwd == str(tmp_path)

    def test_self_record_miss_no_claude_pid(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: (None, "env-miss:absent"),
        )

        assert hr.self_record() is None

    def test_self_record_miss_file_absent(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: ((9999, 0.0), "env-hit"),
        )

        assert hr.self_record() is None

    def test_self_record_malformed_yields_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "555.json").write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: ((555, 0.0), "env-hit"),
        )

        assert hr.self_record() is None


class TestCwdField:
    def test_cwd_parsed(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        payload = {"sessionId": "sess-cwd", "pid": 1, "procStart": ticks, "cwd": "/some/dir"}
        (sessions_dir / "1.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        record = hr.lookup("sess-cwd")
        assert record is not None
        assert record.cwd == "/some/dir"

    def test_cwd_absent_defaults_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        _write_record(sessions_dir, "1.json", "sess-nocwd", 1, ticks)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        record = hr.lookup("sess-nocwd")
        assert record is not None
        assert record.cwd is None


class TestSingleScanInvariant:
    def test_snapshot_performs_exactly_one_directory_scan(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        _write_record(sessions_dir, "1.json", "sess-a", 1, ticks)
        _write_record(sessions_dir, "2.json", "sess-b", 2, ticks)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        from pathlib import Path

        real_glob = Path.glob
        call_count = {"n": 0}

        def counting_glob(self, pattern):
            call_count["n"] += 1
            return real_glob(self, pattern)

        monkeypatch.setattr(Path, "glob", counting_glob)

        result = hr.snapshot()

        assert call_count["n"] == 1
        assert set(result) == {"sess-a", "sess-b"}
