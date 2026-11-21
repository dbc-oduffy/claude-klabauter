"""Tests for group_em/idle_capacity.scan: priority wait plus capable idle box yields an alert."""

from __future__ import annotations

import pytest

from coordinator_core.group_em import idle_capacity as ic

pytestmark = pytest.mark.complete

NOW = 10_000.0
HOLDER = {"session_id": "group-em-de", "live": True}
PEAKS = {"gpu-job": {"ram_mb": 8000, "vram_mib": 20000, "cores": 4, "samples": 3}}


def _box(box_id="machine-a", util=1.0, vram_total=32768, vram_used=6144, stale=False, **extra):
    row = {
        "box_id": box_id, "stale": stale, "age_s": 5.0, "os": "Windows", "load_pct": 3.0,
        "cores_physical": 24, "ram_total_mb": 131072, "ram_avail_mb": 56000,
        "gpus": [{"index": 0, "name": "g", "vram_total_mib": vram_total, "vram_used_mib": vram_used, "util_pct": util}],
    }
    row.update(extra)
    return row


def _wait(**extra):
    row = {"run": "r1", "job_type": "gpu-job", "requester_sid": "req-1", "priority": "priority", "since": NOW - 600}
    row.update(extra)
    return row


def test_priority_wait_and_idle_capable_box_yields_one_alert() -> None:
    alerts = ic.scan([_wait(box_id="casper")], [_box("casper", util=99, vram_used=30000), _box("machine-a")], PEAKS, HOLDER, now=NOW)
    assert len(alerts) == 1
    a = alerts[0]
    assert (a["run"], a["requester_sid"], a["holder_sid"], a["idle_box_id"]) == ("r1", "req-1", "group-em-de", "machine-a")
    assert a["waited_s"] == 600


def test_no_alert_when_the_waiting_box_is_the_only_capable_one() -> None:
    assert ic.scan([_wait(box_id="machine-a")], [_box("machine-a")], PEAKS, HOLDER, now=NOW) == []


def test_no_alert_when_box_is_busy() -> None:
    assert ic.scan([_wait()], [_box(util=ic.IDLE_UTIL_PCT)], PEAKS, HOLDER, now=NOW) == []
    assert len(ic.scan([_wait()], [_box(util=ic.IDLE_UTIL_PCT - 0.1)], PEAKS, HOLDER, now=NOW)) == 1


def test_no_alert_when_headroom_misses_the_measured_peak() -> None:
    assert ic.scan([_wait()], [_box(vram_used=20000)], PEAKS, HOLDER, now=NOW) == []
    assert ic.scan([_wait()], [_box(ram_avail_mb=7999)], PEAKS, HOLDER, now=NOW) == []
    assert ic.scan([_wait()], [_box(load_pct=90.0, util=1.0)], PEAKS, HOLDER, now=NOW) == []


def test_no_alert_for_stale_sample_or_unmeasured_peak_or_no_gpu() -> None:
    assert ic.scan([_wait()], [_box(stale=True)], PEAKS, HOLDER, now=NOW) == []
    assert ic.scan([_wait(job_type="never-run")], [_box()], PEAKS, HOLDER, now=NOW) == []
    nogpu = {"box_id": "mac", "stale": False, "load_pct": 1.0, "gpu_unavailable": "nvml-absent", "ram_avail_mb": 99999}
    assert ic.scan([_wait()], [nogpu], PEAKS, HOLDER, now=NOW) == []


def test_unmeasured_gpu_util_never_counts_as_idle_for_a_vram_job() -> None:
    assert ic.scan([_wait()], [_box(util=None)], PEAKS, HOLDER, now=NOW) == []
    cpu_peaks = {"cpu-job": {"ram_mb": 1000, "cores": 2}}
    assert len(ic.scan([_wait(job_type="cpu-job")], [_box(util=None)], cpu_peaks, HOLDER, now=NOW)) == 1


def test_non_priority_and_young_waits_do_not_alert() -> None:
    assert ic.scan([_wait(priority="normal")], [_box()], PEAKS, HOLDER, now=NOW) == []
    assert ic.scan([_wait(priority=None)], [_box()], PEAKS, HOLDER, now=NOW) == []
    assert ic.scan([_wait(since=NOW - ic.MIN_WAIT_S + 1)], [_box()], PEAKS, HOLDER, now=NOW) == []
    assert len(ic.scan([_wait(since=NOW - ic.MIN_WAIT_S)], [_box()], PEAKS, HOLDER, now=NOW)) == 1


def test_cpu_only_job_judged_on_load_not_gpu() -> None:
    peaks = {"cpu-job": {"ram_mb": 1000, "cores": 2}}
    wait = _wait(job_type="cpu-job")
    assert len(ic.scan([wait], [_box(gpus=[], util=0)], peaks, HOLDER, now=NOW)) == 1
    assert ic.scan([wait], [_box(load_pct=50.0)], peaks, HOLDER, now=NOW) == []


def test_picks_the_least_utilised_capable_box_and_dedupes_runs() -> None:
    boxes = [_box("a", util=8.0), _box("b", util=2.0)]
    alerts = ic.scan([_wait(), _wait()], boxes, PEAKS, HOLDER, now=NOW)
    assert [x["idle_box_id"] for x in alerts] == ["b"]


def test_missing_holder_names_none_and_garbage_never_raises() -> None:
    assert ic.scan([_wait()], [_box()], PEAKS, None, now=NOW)[0]["holder_sid"] is None
    assert ic.scan(None, None, None, None) == []
    assert ic.scan([_wait(since="x"), "junk", {}], [None, {"gpus": 5}], PEAKS, HOLDER, now=NOW) == []


def test_scan_sends_nothing_and_does_not_mutate_inputs() -> None:
    waits, samples = [_wait()], [_box()]
    import copy

    before = copy.deepcopy((waits, samples, PEAKS))
    ic.scan(waits, samples, PEAKS, HOLDER, now=NOW)
    assert (waits, samples, PEAKS) == before


def test_held_unstarted_grant_makes_the_box_not_idle() -> None:
    held = {"machine-a": [{"run": "g1", "box_id": "machine-a", "vram_mib": 20000, "ram_mb": 8000, "cores": 4}]}
    assert ic.scan([_wait()], [_box()], PEAKS, HOLDER, now=NOW, held=held) == []
    assert len(ic.scan([_wait()], [_box()], PEAKS, HOLDER, now=NOW, held={"machine-a": []})) == 1
    assert len(ic.scan([_wait()], [_box()], PEAKS, HOLDER, now=NOW)) == 1
