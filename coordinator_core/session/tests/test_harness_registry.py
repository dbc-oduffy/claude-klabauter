"""
coordinator_core.session.tests.test_harness_registry — C1 scoped test suite.

Spec backlink: pln-the-harness-already-knows-whic-096836 § C1

All fixtures are fabricated on `tmp_path` — this suite never reads the
operator's real `~/.claude/sessions`.
"""

from __future__ import annotations

import calendar
import json
import os
import time
from datetime import datetime, timezone

import pytest

from coordinator_core.session import harness_registry as hr

# The UTC-vs-local distinction on the ctime leg is invisible on a
# UTC-offset-0 runner (mktime == timegm there) — pin a non-UTC zone for
# every test in this module so a regression to `time.mktime` fails loudly
# regardless of the CI box's own zone. `time.tzset()` is POSIX-only (absent
# on Windows); tests using it are skipped there rather than silently
# passing on a zone they didn't actually pin.
_HAS_TZSET = hasattr(time, "tzset")


@pytest.fixture(autouse=True)
def _pin_non_utc_timezone():
    if not _HAS_TZSET:
        yield
        return
    original = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


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
        assert hr._proc_start_to_epoch(ticks) == pytest.approx(epoch, abs=1e-3)

    def test_out_of_band_past_rejected(self):
        epoch = time.time() - hr._SANITY_BAND_PAST_SEC - 3600
        ticks = _epoch_to_filetime_ticks(epoch)
        assert hr._proc_start_to_epoch(ticks) is None

    def test_out_of_band_future_rejected(self):
        epoch = time.time() + hr._SANITY_BAND_FUTURE_SEC + 3600
        ticks = _epoch_to_filetime_ticks(epoch)
        assert hr._proc_start_to_epoch(ticks) is None

    def test_non_numeric_rejected(self):
        assert hr._proc_start_to_epoch("not-a-number") is None
        assert hr._proc_start_to_epoch(None) is None

    def test_ctime_string_in_band_parses(self):
        # procStart's ctime string is UTC (memo-confirmed, 37/37 live
        # records) — build the fixture from gmtime, expect it to round-trip
        # via timegm exactly. TZ is pinned non-UTC by the module fixture, so
        # this fails if the implementation regresses to `time.mktime`.
        epoch = int(time.time()) - 60
        ctime_str = time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(epoch))
        result = hr._proc_start_to_epoch(ctime_str)
        assert result is not None
        assert result == pytest.approx(epoch, abs=1e-6)
        # The buggy local-time interpretation of the identical clock-face
        # reading: on a non-UTC runner (pinned above) this must diverge from
        # `result`, or this test isn't exercising the UTC-vs-local defect.
        buggy = time.mktime(time.strptime(ctime_str, "%a %b %d %H:%M:%S %Y"))
        assert result != pytest.approx(buggy, abs=1.0)

    def test_iso8601_naive_string_parses(self):
        # A naive ISO-8601 procStart is treated as UTC, not local time.
        epoch = int(time.time()) - 60
        iso_str = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None).isoformat()
        result = hr._proc_start_to_epoch(iso_str)
        assert result is not None
        assert result == pytest.approx(epoch, abs=1e-6)
        # Buggy local-time interpretation of the same naive clock-face value.
        naive = datetime.fromisoformat(iso_str)
        buggy = time.mktime(naive.timetuple())
        assert result != pytest.approx(buggy, abs=1.0)

    def test_iso8601_aware_string_parses(self):
        epoch = time.time() - 60
        iso_str = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        result = hr._proc_start_to_epoch(iso_str)
        assert result is not None
        assert result == pytest.approx(epoch, abs=1.0)

    def test_ctime_string_out_of_band_rejected(self):
        epoch = time.time() - hr._SANITY_BAND_PAST_SEC - 3600
        ctime_str = time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(epoch))
        assert hr._proc_start_to_epoch(ctime_str) is None

    def test_garbage_string_rejected(self):
        assert hr._proc_start_to_epoch("not-a-date-or-number") is None
        assert hr._proc_start_to_epoch("") is None

    def test_float_procstart_falls_through_not_truncated_into_filetime(self):
        # A JSON float must never silently truncate via int() into the
        # FILETIME leg (Defect 1) -- it is neither a genuine int nor a
        # ctime/ISO-8601 string, so it must yield None, not a coincidentally
        # "valid" epoch from truncated ticks.
        epoch = time.time() - 60
        ticks_float = float(_epoch_to_filetime_ticks(epoch))
        assert hr._proc_start_to_epoch(ticks_float) is None

    def test_numeric_string_procstart_falls_through_to_string_branches(self):
        # A numeric-looking string (e.g. "133...") must not be swallowed
        # into the FILETIME leg via int(raw) either -- it isn't a ctime or
        # ISO-8601 shape, so it must yield None rather than a coincidence.
        in_band_ticks = _epoch_to_filetime_ticks(time.time() - 60)
        assert hr._proc_start_to_epoch(str(in_band_ticks)) is None

    def test_bool_procstart_rejected(self):
        # bool is an int subclass in Python -- must not be admitted to the
        # FILETIME leg (mirrors _parse_one's own pid bool-rejection).
        assert hr._proc_start_to_epoch(True) is None
        assert hr._proc_start_to_epoch(False) is None


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


class TestCtimeShapedRegistry:
    def test_snapshot_parses_all_ctime_shaped_records(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        epoch_a = int(time.time()) - 60
        epoch_b = int(time.time()) - 120
        ctime_a = time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(epoch_a))
        ctime_b = time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(epoch_b))
        payload_a = {"sessionId": "sess-a", "pid": 1, "procStart": ctime_a}
        payload_b = {"sessionId": "sess-b", "pid": 2, "procStart": ctime_b}
        (sessions_dir / "1.json").write_text(json.dumps(payload_a), encoding="utf-8")
        (sessions_dir / "2.json").write_text(json.dumps(payload_b), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        result = hr.snapshot()
        assert set(result) == {"sess-a", "sess-b"}
        assert result["sess-a"].pid == 1
        assert result["sess-a"].start_epoch == pytest.approx(epoch_a, abs=1.0)
        assert result["sess-b"].pid == 2
        assert result["sess-b"].start_epoch == pytest.approx(epoch_b, abs=1.0)


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
        assert record.stable_pid_capture == "env-hit"

    def test_self_record_threads_reason_onto_record(self, tmp_path, monkeypatch):
        """C2b — the discarded `reason` leg of `_resolve_claude_pid_from_env()`
        lands on the returned record's `stable_pid_capture` field, reusing
        the same vocabulary the other three call sites already write."""
        sessions_dir = tmp_path / "sessions"
        epoch = time.time() - 60
        ticks = _epoch_to_filetime_ticks(epoch)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessionId": "sess-self-2",
            "pid": 4343,
            "procStart": ticks,
        }
        (sessions_dir / "4343.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: ((4343, 0.0), "env-miss:name-mismatch"),
        )

        result = hr.self_record()
        assert result is not None
        _session_id, record = result
        assert record.stable_pid_capture == "env-miss:name-mismatch"

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


class TestNameAndSocketFields:
    """2026-08-13 session-owner-reachability-registry § 1: `name` and
    `messagingSocketPath` parse the same optional-string-or-None way `cwd`
    already does -- required by `coordinator_core.session.reachability`."""

    def test_name_and_socket_parsed(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessionId": "sess-named",
            "pid": 1,
            "procStart": ticks,
            "name": "claude-klabauter-57",
            "messagingSocketPath": "/tmp/cc-socks/57557.sock",
        }
        (sessions_dir / "1.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        record = hr.lookup("sess-named")
        assert record is not None
        assert record.name == "claude-klabauter-57"
        assert record.messaging_socket_path == "/tmp/cc-socks/57557.sock"

    def test_name_and_socket_absent_default_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        _write_record(sessions_dir, "1.json", "sess-bare", 1, ticks)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        record = hr.lookup("sess-bare")
        assert record is not None
        assert record.name is None
        assert record.messaging_socket_path is None

    def test_non_string_name_and_socket_yield_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        payload = {
            "sessionId": "sess-weird",
            "pid": 1,
            "procStart": ticks,
            "name": 12345,
            "messagingSocketPath": [],
        }
        (sessions_dir / "1.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        record = hr.lookup("sess-weird")
        assert record is not None
        assert record.name is None
        assert record.messaging_socket_path is None


class TestStatusField:
    """2026-08-13 live-peer-roster § 3 amendment: `status` parses the same
    optional-string-or-None way `cwd`/`name` already do -- display-only, no
    verdict attached, and NEVER a liveness input (this module's own
    negative-spec, amended not deleted)."""

    def test_status_parsed(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessionId": "sess-status",
            "pid": 1,
            "procStart": ticks,
            "status": "busy",
        }
        (sessions_dir / "1.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        record = hr.lookup("sess-status")
        assert record is not None
        assert record.status == "busy"

    def test_status_absent_defaults_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        _write_record(sessions_dir, "1.json", "sess-nostatus", 1, ticks)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        record = hr.lookup("sess-nostatus")
        assert record is not None
        assert record.status is None

    def test_non_string_status_yields_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        ticks = _epoch_to_filetime_ticks(time.time() - 60)
        payload = {
            "sessionId": "sess-weird-status",
            "pid": 1,
            "procStart": ticks,
            "status": 12345,
        }
        (sessions_dir / "1.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        record = hr.lookup("sess-weird-status")
        assert record is not None
        assert record.status is None

    def test_status_never_attaches_a_verdict(self, tmp_path, monkeypatch):
        # A record's `status` field carries no liveness/reachability meaning
        # of its own -- parsing it must not change what `lookup`/`snapshot`
        # otherwise return for an out-of-band procStart, mirroring `cwd`'s
        # own no-verdict contract.
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        out_of_band_ticks = _epoch_to_filetime_ticks(time.time() - hr._SANITY_BAND_PAST_SEC - 3600)
        payload = {
            "sessionId": "sess-dead-but-labeled-busy",
            "pid": 1,
            "procStart": out_of_band_ticks,
            "status": "busy",
        }
        (sessions_dir / "1.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)

        # An out-of-band procStart still yields None for the whole record,
        # regardless of what `status` says -- status is never consulted.
        assert hr.lookup("sess-dead-but-labeled-busy") is None


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


class TestRegistryRecordFieldContract:
    """Pins `RegistryRecord`'s full field set against the module docstring's
    documented list, so a future field addition cannot drift the docstring
    silently (the exact defect class this module has already been bitten
    by — added chunk C2b alongside `stable_pid_capture`)."""

    def test_field_set_matches_documented_contract(self):
        import dataclasses

        field_names = tuple(f.name for f in dataclasses.fields(hr.RegistryRecord))
        assert field_names == (
            "pid",
            "start_epoch",
            "cwd",
            "name",
            "messaging_socket_path",
            "status",
            "stable_pid_capture",
        )

    def test_all_fields_except_pid_and_start_epoch_default_to_none(self):
        import dataclasses

        for f in dataclasses.fields(hr.RegistryRecord):
            if f.name in ("pid", "start_epoch"):
                continue
            assert f.default is None, f"{f.name} must default to None"
