"""Pins the falsifier's safety fences and its measured shape (capped tree, one-close reap)."""

import sys

import pytest

from coordinator_core.benchmarks import spawn_guard_overhead as sgo
from coordinator_core.testing import nested_session_guard

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Job Object measurement")


@windows_only
@pytest.mark.parametrize("bad", [0, 1, -1, sgo.MAX_SAFE_LIMIT + 1, 10_000])
def test_limit_outside_safe_range_is_refused_before_any_spawn(bad):
    with pytest.raises(ValueError):
        sgo._create_job(bad)


@windows_only
def test_limit_inside_safe_range_is_applied_and_read_back():
    job = sgo._create_job(sgo.DEFAULT_LIMIT)
    try:
        assert sgo._read_active_limit(job) == sgo.DEFAULT_LIMIT - 1
    finally:
        sgo._k.CloseHandle(job)


@windows_only
def test_fallback_job_without_create_capped_job_still_applies_and_reads_back_the_limit(monkeypatch):
    from coordinator_core.diagnostics import contained_run

    monkeypatch.delattr(contained_run, "create_capped_job")
    job = sgo._create_job(sgo.DEFAULT_LIMIT)
    try:
        assert sgo._read_active_limit(job) == sgo.DEFAULT_LIMIT - 1
    finally:
        sgo._k.CloseHandle(job)
    with pytest.raises(ValueError):
        sgo._create_job(sgo.MAX_SAFE_LIMIT + 1)


@windows_only
def test_run_reports_capped_tree_and_zero_survivors_after_one_close():
    result = sgo.run(sgo.DEFAULT_LIMIT)
    assert result["supported"] is True
    assert 2 <= result["max_tree"] <= sgo.DEFAULT_LIMIT
    assert result["survivors"] == 0
    assert result["guard_present"] is True
    assert result["nested_exit"] == nested_session_guard.REFUSAL_CODE
    assert result["added_spawns"] == 0
    assert result["setup_ms"] >= 0


def test_non_windows_reports_unsupported(monkeypatch):
    monkeypatch.setattr(sgo, "IS_WINDOWS", False)
    assert sgo.run() == {"supported": False, "platform": sys.platform}
