"""Tests for coordinator_core.warm.telemetry.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C26
"""

from __future__ import annotations

import json
import threading

import pytest

from coordinator_core.warm import telemetry


def test_record_invocation_counts_warm_and_cold_separately():
    t = telemetry.ServerTelemetry()
    t.record_invocation(warm=True)
    t.record_invocation(warm=True)
    t.record_invocation(warm=False)

    snap = t.snapshot()
    assert snap["served_count"] == 3
    assert snap["warm_count"] == 2
    assert snap["cold_count"] == 1


def test_record_invocation_returns_running_served_count():
    t = telemetry.ServerTelemetry()
    assert t.record_invocation(warm=True) == 1
    assert t.record_invocation(warm=False) == 2
    assert t.record_invocation(warm=True) == 3


def test_served_count_is_zero_arg_and_matches_snapshot():
    t = telemetry.ServerTelemetry()
    t.record_invocation(warm=True)
    t.record_invocation(warm=True)

    assert t.served_count() == 2 == t.snapshot()["served_count"]


def test_served_count_binds_directly_into_idle_served_count_fn():
    """`served_count` must satisfy `idle.ServedCountFn` -- a zero-arg
    callable returning the served-invocation count -- with no adapter,
    per this module's own docstring."""
    from coordinator_core.warm import idle

    t = telemetry.ServerTelemetry()
    t.record_invocation(warm=True)

    served_count: idle.ServedCountFn = t.served_count
    assert served_count() == 1


def test_record_exit_accepts_each_known_reason():
    for reason in telemetry.EXIT_REASONS:
        t = telemetry.ServerTelemetry()
        t.record_exit(reason)
        assert t.snapshot()["exit_reason"] == reason


def test_record_exit_rejects_unknown_reason():
    t = telemetry.ServerTelemetry()
    with pytest.raises(ValueError):
        t.record_exit("some-other-reason")


def test_record_exit_first_call_wins():
    t = telemetry.ServerTelemetry()
    t.record_exit(telemetry.EXIT_REASON_SKEW)
    t.record_exit(telemetry.EXIT_REASON_IDLE_DEMOTION)

    assert t.snapshot()["exit_reason"] == telemetry.EXIT_REASON_SKEW


def test_snapshot_before_any_exit_has_none_reason():
    t = telemetry.ServerTelemetry()
    assert t.snapshot()["exit_reason"] is None


def test_snapshot_life_seconds_advances_with_injected_clock():
    ticks = iter([100.0, 137.5])
    t = telemetry.ServerTelemetry(clock=lambda: next(ticks))

    assert t.snapshot()["life_seconds"] == pytest.approx(37.5)


def test_record_invocation_is_thread_safe_under_concurrency():
    t = telemetry.ServerTelemetry()
    threads = [threading.Thread(target=t.record_invocation, kwargs={"warm": True}) for _ in range(50)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert t.served_count() == 50


def test_telemetry_path_lives_in_the_clone_s_runtime_dir(tmp_path):
    """Telemetry follows `breadcrumb.svc_dir` wherever it resolves — the two
    are deliberately one seam, so the 2026-08-19 move out of the engine
    clone carried both files rather than splitting them."""
    from coordinator_core.warm.breadcrumb import svc_dir

    path = telemetry.telemetry_path(tmp_path)
    assert path == svc_dir(tmp_path) / telemetry.TELEMETRY_FILENAME
    assert tmp_path not in path.parents


def test_flush_appends_one_json_line_with_snapshot_fields(tmp_path):
    t = telemetry.ServerTelemetry()
    t.record_invocation(warm=True)
    t.record_invocation(warm=False)
    t.record_exit(telemetry.EXIT_REASON_IDLE_DEMOTION)

    t.flush(engine_root=tmp_path)

    path = telemetry.telemetry_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["served_count"] == 2
    assert record["warm_count"] == 1
    assert record["cold_count"] == 1
    assert record["exit_reason"] == telemetry.EXIT_REASON_IDLE_DEMOTION
    assert "flushed_at" in record
    assert "life_seconds" in record


def test_flush_appends_across_multiple_server_lives(tmp_path):
    first = telemetry.ServerTelemetry()
    first.record_invocation(warm=True)
    first.record_exit(telemetry.EXIT_REASON_SKEW)
    first.flush(engine_root=tmp_path)

    second = telemetry.ServerTelemetry()
    second.record_invocation(warm=False)
    second.record_exit(telemetry.EXIT_REASON_OPERATOR_STOP)
    second.flush(engine_root=tmp_path)

    path = telemetry.telemetry_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    reasons = [json.loads(line)["exit_reason"] for line in lines]
    assert reasons == [telemetry.EXIT_REASON_SKEW, telemetry.EXIT_REASON_OPERATOR_STOP]


def test_flush_creates_svc_dir_when_absent(tmp_path):
    t = telemetry.ServerTelemetry()
    t.flush(engine_root=tmp_path)

    assert telemetry.telemetry_path(tmp_path).exists()


def test_flush_never_raises_on_write_failure(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry.Path, "mkdir", _boom)

    t = telemetry.ServerTelemetry()
    t.flush(engine_root=tmp_path)  # must not raise
