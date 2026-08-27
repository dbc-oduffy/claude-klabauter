"""Tests for coordinator_core.session.context_usage_sidecar.

Covers C1 of the 2026-08-17 "the advisory reads the harness" plan: the
context-usage sidecar's single-source path resolver, atomic writer, and
reader — round-trip, absent file, unparseable/truncated file, age
computation, write elision, and atomicity under a simulated interrupted
write.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from coordinator_core.session import context_usage_sidecar as sidecar_module
from coordinator_core.session.context_usage_sidecar import (
    UsageReading,
    read_usage,
    sidecar_path,
    write_usage,
)


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    """Sandbox every test into its own tempdir and clear the in-process
    write-elision memo, so tests don't leak state across each other."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    (tmp_path / "state" / "context-window").mkdir(parents=True, exist_ok=True)
    sidecar_module._last_written.clear()
    yield


def test_sidecar_path_resolves_the_producers_settings_home(monkeypatch, tmp_path):
    """The path is the producer's, not a tempdir.

    Regression pin: this resolver was built against a claude-klabauter-side producer
    that was withdrawn before shipping, leaving it pointed at
    `tempfile.gettempdir()/context-usage-<sid>` — a file nothing writes. The
    live producer is DoE-claude's statusline, publishing to
    the settings home's `state/context-window/` directory, one file per sid.
        A reader on
    the wrong path fails silently, so this asserts the shape end to end.
    """
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    result = sidecar_path("abc123")
    assert result == tmp_path / "state" / "context-window" / "abc123.json"
    assert "context-usage-" not in str(result)


def test_sidecar_path_defaults_to_the_conventional_settings_home(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    result = sidecar_path("abc123")
    assert result.parent.parent.parent.name == ".coordinator-claude-settings"
    assert result.name == "abc123.json"


def test_sidecar_path_sanitises_the_session_id(monkeypatch, tmp_path):
    """Matches the producer's own `_safe_stem`. A separator that survived here
    would name a file in another directory entirely."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    result = sidecar_path("../../evil/ab c1_2-3")
    assert result.name == "evilabc1_2-3.json"
    assert result.parent == tmp_path / "state" / "context-window"


def test_sidecar_path_is_keyed_on_session_id():
    a = sidecar_path("session-a")
    b = sidecar_path("session-b")

    assert a != b
    assert a.name == "session-a.json"
    assert b.name == "session-b.json"


def test_round_trip_write_then_read():
    session_id = "sess-round-trip"
    block = {
        "used_percentage": 42.0,
        "remaining_percentage": 58.0,
        "context_window_size": 200_000,
        "current_usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 50,
            "cache_read_input_tokens": 25,
        },
    }

    write_usage(session_id, block, now=1000.0)
    reading = read_usage(session_id, now=1005.0)

    assert reading is not None
    assert reading.context_window == block
    assert reading.age_seconds == pytest.approx(5.0)


def test_read_usage_absent_file_returns_none():
    reading = read_usage("sess-never-written", now=1000.0)

    assert reading is None


def test_read_usage_unparseable_json_returns_none():
    session_id = "sess-garbage"
    target = sidecar_path(session_id)
    target.write_text("{not valid json")

    reading = read_usage(session_id, now=1000.0)

    assert reading is None


def test_read_usage_truncated_file_returns_none():
    session_id = "sess-truncated"
    write_usage(session_id, {"used_percentage": 10.0}, now=1000.0)
    target = sidecar_path(session_id)

    full_bytes = target.read_bytes()
    target.write_bytes(full_bytes[: len(full_bytes) // 2])

    reading = read_usage(session_id, now=1000.0)

    assert reading is None


def test_read_usage_missing_context_window_key_returns_none():
    session_id = "sess-wrong-shape"
    target = sidecar_path(session_id)
    target.write_text('{"captured_at": 1000.0}')

    reading = read_usage(session_id, now=1000.0)

    assert reading is None


def test_read_usage_missing_captured_at_key_returns_none():
    session_id = "sess-no-captured-at"
    target = sidecar_path(session_id)
    target.write_text('{"context_window": {"used_percentage": 10.0}}')

    reading = read_usage(session_id, now=1000.0)

    assert reading is None


def test_age_seconds_computed_against_injected_now():
    session_id = "sess-age"
    write_usage(session_id, {"used_percentage": 10.0}, now=500.0)

    reading_immediate = read_usage(session_id, now=500.0)
    reading_later = read_usage(session_id, now=800.0)

    assert reading_immediate is not None
    assert reading_immediate.age_seconds == pytest.approx(0.0)
    assert reading_later is not None
    assert reading_later.age_seconds == pytest.approx(300.0)


def test_write_usage_is_atomic_no_temp_file_left_behind():
    session_id = "sess-atomic"
    write_usage(session_id, {"used_percentage": 10.0}, now=1000.0)

    target = sidecar_path(session_id)
    siblings = list(target.parent.iterdir())

    assert siblings == [target]


def test_reader_sees_prior_good_record_not_a_torn_write():
    """Simulates an interrupted write: a partial temp file sits alongside a
    prior good sidecar record. The reader must still return the prior good
    record rather than any partial content -- write_usage only ever makes
    the good record visible via os.replace, so a stray temp file left by a
    simulated crash must never be read as the sidecar itself."""
    session_id = "sess-interrupted"
    good_block = {"used_percentage": 10.0}
    write_usage(session_id, good_block, now=1000.0)

    target = sidecar_path(session_id)
    partial_temp = target.parent / f".{target.name}.deadbeef.tmp"
    partial_temp.write_bytes(b'{"context_window": {"used_pe')

    reading = read_usage(session_id, now=1000.0)

    assert reading is not None
    assert reading.context_window == good_block


def test_write_elision_skips_identical_write(monkeypatch):
    session_id = "sess-elide"
    block = {"used_percentage": 10.0}

    write_usage(session_id, block, now=1000.0)
    target = sidecar_path(session_id)
    first_mtime = target.stat().st_mtime_ns

    write_usage(session_id, dict(block), now=2000.0)
    second_mtime = target.stat().st_mtime_ns

    assert second_mtime == first_mtime

    reading = read_usage(session_id, now=2000.0)
    assert reading is not None
    assert reading.age_seconds == pytest.approx(1000.0)


def test_write_elision_does_not_apply_across_different_blocks():
    session_id = "sess-no-elide-on-change"
    write_usage(session_id, {"used_percentage": 10.0}, now=1000.0)
    target = sidecar_path(session_id)
    first_mtime = target.stat().st_mtime_ns

    write_usage(session_id, {"used_percentage": 20.0}, now=1001.0)
    second_mtime = target.stat().st_mtime_ns

    assert second_mtime != first_mtime

    reading = read_usage(session_id, now=1001.0)
    assert reading is not None
    assert reading.context_window == {"used_percentage": 20.0}
