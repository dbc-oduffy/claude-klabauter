"""Tests for coordinator_core.group_em.capacity_admit; every case writes under tmp_path only."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.group_em import capacity_admit as ca
from coordinator_core.group_em import capacity_record

NOW = 1_000_000.0


def _sample(vram_total=32768, vram_used=6144, ram_avail=55_000, cores=24, load=10.0, jobs=None, **extra):
    s = {
        "box_id": "machine-a",
        "os": "Windows",
        "cores_logical": cores,
        "load_pct": load,
        "ram_total_mb": 131_072,
        "ram_avail_mb": ram_avail,
        "gpus": [{"index": 0, "name": "5090", "vram_total_mib": vram_total, "vram_used_mib": vram_used, "util_pct": 1}],
        "jobs": jobs or [],
        "stale": False,
        "age_s": 5.0,
    }
    s.update(extra)
    return s


def test_32gb_box_with_6gb_used_grants_a_20gb_job() -> None:
    reply = ca.decide(_sample(), {"vram_mib": 20000, "ram_mb": 8000, "cores": 2}, [], now=NOW)
    assert reply["grant"] is True and reply["short"] == []
    assert reply["headroom"]["vram_mib"] == 32768 - 6144
    assert reply["peak_basis"] == "measured" and reply["gpu_index"] == 0


def test_two_jobs_whose_summed_peaks_fit_both_grant_and_the_third_refuses_naming_vram(tmp_path: Path) -> None:
    sample = _sample()
    peak = {"vram_mib": 10000, "ram_mb": 4000, "cores": 1}
    first = ca.admit("r1", "t", sample, peak, home=tmp_path, now=NOW)
    second = ca.admit("r2", "t", sample, peak, home=tmp_path, now=NOW)
    third = ca.admit("r3", "t", sample, peak, home=tmp_path, requester_sid="s3", priority="high", now=NOW)
    assert first["grant"] and second["grant"]
    assert second["headroom"]["vram_mib"] == 32768 - 6144 - 10000
    assert third["grant"] is False
    assert third["short"][0]["resource"] == "vram"
    assert third["short"][0]["need"] == 10000 and third["short"][0]["free"] == 32768 - 6144 - 20000
    assert [r["run"] for r in ca.read_held(sample, tmp_path, NOW)] == ["r1", "r2"]


def test_no_reply_carries_a_slot_field(tmp_path: Path) -> None:
    sample = _sample()
    replies = [
        ca.decide(sample, {"vram_mib": 1}, [], now=NOW),
        ca.decide(sample, {"vram_mib": 10**9}, [], now=NOW),
        ca.decide(sample, None, [], now=NOW),
        ca.admit("r", "t", sample, {"vram_mib": 1}, home=tmp_path, now=NOW),
    ]
    for reply in replies:
        assert "slot" not in json.dumps(reply).lower()


def test_refusal_names_every_short_axis_with_need_and_free() -> None:
    sample = _sample(ram_avail=1000, load=50.0, cores=4)
    reply = ca.decide(sample, {"vram_mib": 40000, "ram_mb": 2000, "cores": 3}, [], now=NOW)
    assert [s["resource"] for s in reply["short"]] == ["vram", "ram", "cores"]
    assert reply["short"][1] == {"resource": "ram", "need": 2000.0, "free": 1000.0}
    assert reply["short"][2]["free"] == 2.0


def test_unmeasured_peak_refuses_as_measurement_unless_declared() -> None:
    sample = _sample()
    refused = ca.decide(sample, None, [], now=NOW)
    assert refused["grant"] is False
    assert refused["short"] == [{"resource": "measurement", "reason": "no_measured_peak"}]
    declared = ca.decide(sample, None, [], declared={"vram_mib": 4000}, now=NOW)
    assert declared["grant"] is True and declared["peak_basis"] == "declared"
    measured_wins = ca.decide(sample, {"vram_mib": 100}, [], declared={"vram_mib": 10**9}, now=NOW)
    assert measured_wins["grant"] is True and measured_wins["peak_basis"] == "measured"


def test_stale_sample_refuses_as_measurement() -> None:
    reply = ca.decide(_sample(stale=True), {"vram_mib": 1}, [], now=NOW)
    assert reply["grant"] is False and reply["short"] == [{"resource": "measurement", "reason": "no_fresh_sample"}]


def test_gpu_unavailable_names_vram_with_the_reason_and_ram_only_jobs_still_grant() -> None:
    sample = _sample()
    sample.pop("gpus")
    sample["gpu_unavailable"] = "nvml-absent"
    vram = ca.decide(sample, {"vram_mib": 100}, [], now=NOW)
    assert vram["short"] == [{"resource": "vram", "need": 100.0, "free": None, "reason": "nvml-absent"}]
    assert ca.decide(sample, {"ram_mb": 100}, [], now=NOW)["grant"] is True


def test_gpu_rows_without_used_vram_name_the_unmeasured_usage() -> None:
    sample = _sample()
    sample["gpus"] = [{"index": 0, "vram_total_mib": 24576, "vram_used_mib": None, "util_pct": None, "unified": True}]
    reply = ca.decide(sample, {"vram_mib": 100}, [], now=NOW)
    assert reply["grant"] is False
    assert reply["short"] == [{"resource": "vram", "need": 100.0, "free": None, "reason": ca.GPU_USAGE_UNMEASURED}]


def test_held_grant_stops_counting_once_its_run_shows_in_jobs(tmp_path: Path) -> None:
    peak = {"vram_mib": 20000}
    assert ca.admit("r1", "t", _sample(), peak, home=tmp_path, now=NOW)["grant"]
    assert ca.admit("r2", "t", _sample(), peak, home=tmp_path, now=NOW)["grant"] is False
    # r1 now runs: its VRAM is in vram_used, so the held row must not be subtracted twice.
    running = _sample(vram_used=6144 + 20000, jobs=[{"run": "r1", "job_type": "t"}])
    assert ca.read_held(running, tmp_path, NOW) == []
    assert ca.decide(running, {"vram_mib": 1000}, ca.read_held(running, tmp_path, NOW), now=NOW)["grant"]


def test_held_grant_expires_after_ttl(tmp_path: Path) -> None:
    ca.admit("r1", "t", _sample(), {"vram_mib": 20000}, home=tmp_path, now=NOW)
    assert len(ca.read_held(_sample(), tmp_path, NOW + ca.GRANT_TTL_S - 1)) == 1
    assert ca.read_held(_sample(), tmp_path, NOW + ca.GRANT_TTL_S + 1) == []


def test_refusal_appends_one_wait_row_keeping_first_since_and_grant_clears_it(tmp_path: Path) -> None:
    big = {"vram_mib": 99999}
    ca.admit("r9", "t", _sample(), big, home=tmp_path, requester_sid="s", priority="high", now=NOW)
    ca.admit("r9", "t", _sample(), big, home=tmp_path, requester_sid="s", priority="high", now=NOW + 60)
    waits = ca.read_waits(tmp_path, NOW + 61)
    box = _sample().get("box_id")
    assert waits == [
        {"run": "r9", "job_type": "t", "box_id": box, "requester_sid": "s", "priority": "high", "since": NOW, "seen_at": NOW + 60}
    ]
    assert ca.admit("r9", "t", _sample(), {"vram_mib": 1}, home=tmp_path, now=NOW + 90)["grant"]
    assert ca.read_waits(tmp_path, NOW + 91) == []


def test_wait_row_expires_unless_the_run_keeps_asking(tmp_path: Path) -> None:
    big = {"vram_mib": 99999}
    ca.admit("gone", "t", _sample(), big, home=tmp_path, now=NOW)
    ca.admit("live", "t", _sample(), big, home=tmp_path, now=NOW + ca.WAIT_TTL_S - 1)
    assert {w["run"] for w in ca.read_waits(tmp_path, NOW + ca.WAIT_TTL_S - 2)} == {"gone", "live"}
    assert {w["run"] for w in ca.read_waits(tmp_path, NOW + ca.WAIT_TTL_S + 1)} == {"live"}
    ca.admit("other", "t", _sample(), big, home=tmp_path, now=NOW + ca.WAIT_TTL_S + 1)
    assert {w["run"] for w in ca.read_waits(tmp_path, NOW + ca.WAIT_TTL_S + 1)} == {"live", "other"}
    assert "gone" not in tmp_path.joinpath("state/group-em/box/capacity-waits.jsonl").read_text()


def test_two_gpus_grant_on_the_freest_and_held_rows_follow_their_gpu(tmp_path: Path) -> None:
    sample = _sample()
    sample["gpus"] = [
        {"index": 0, "vram_total_mib": 24000, "vram_used_mib": 20000},
        {"index": 1, "vram_total_mib": 24000, "vram_used_mib": 2000},
    ]
    first = ca.admit("a", "t", sample, {"vram_mib": 20000}, home=tmp_path, now=NOW)
    assert first["grant"] and first["gpu_index"] == 1
    second = ca.admit("b", "t", sample, {"vram_mib": 3000}, home=tmp_path, now=NOW)
    assert second["grant"] and second["gpu_index"] == 0 and second["headroom"]["vram_mib"] == 4000


def test_decision_reads_a_stamped_record_end_to_end(tmp_path: Path) -> None:
    capacity_record.stamp(_sample(), tmp_path, now=NOW)
    record = capacity_record.read({"box_id": "machine-a", "root": tmp_path}, now=NOW + 5)
    reply = ca.admit("r", "t", record, {"vram_mib": 20000, "ram_mb": 8000}, home=tmp_path, now=NOW + 5)
    assert reply["grant"] is True and reply["recorded"] is True


def test_ledger_failure_refuses_unrecorded_and_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    reply = ca.admit("r", "t", _sample(), {"vram_mib": 1}, home=blocker, now=NOW)
    assert reply["grant"] is False and reply["recorded"] is False
    assert reply["short"] == [{"resource": "measurement", "reason": "ledger_unavailable"}]


def _booked(tmp_path: Path, run: str = "b1", **kw) -> None:
    reply = ca.admit(run, "t", _sample(), {"vram_mib": 1000}, home=tmp_path, now=NOW, pid=4242, **kw)
    assert reply["grant"] and reply["recorded"]


def test_booking_fields_land_on_the_row_and_absent_ones_stay_absent(tmp_path: Path) -> None:
    _booked(tmp_path, pid_started_at=123.0, owner_sid="sid", window_s=900.0, expected_rate=2.5, rate_unit="files/s")
    ca.admit("plain", "t", _sample(), {"vram_mib": 1000}, home=tmp_path, now=NOW)
    rows = {r["run"]: r for r in ca._rows(ca.grants_path(tmp_path))}
    assert {k: rows["b1"][k] for k in ("pid", "pid_started_at", "owner_sid", "window_s", "expected_rate", "rate_unit")} == {
        "pid": 4242, "pid_started_at": 123.0, "owner_sid": "sid", "window_s": 900.0, "expected_rate": 2.5, "rate_unit": "files/s",
    }
    assert not {"pid", "owner_sid", "window_s", "expected_rate", "rate_unit"} & set(rows["plain"])
    assert [r["run"] for r in ca.read_booked(tmp_path)] == ["b1"]  # the plain grant is not pid-booked


def test_booked_window_defaults_to_ttl(tmp_path: Path) -> None:
    _booked(tmp_path)
    assert ca.read_booked(tmp_path)[0]["window_s"] == ca.GRANT_TTL_S


def test_booked_row_survives_a_later_admit_after_start_and_after_ttl_and_headroom_ignores_it(
    tmp_path: Path, monkeypatch
) -> None:
    from coordinator_core.group_em import session_registry

    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: True)
    _booked(tmp_path)
    running = _sample(jobs=[{"run": "b1", "job_type": "t"}])
    later = NOW + ca.GRANT_TTL_S + 10
    reply = ca.admit("r2", "t", running, {"vram_mib": 1000}, home=tmp_path, now=later)
    assert reply["grant"]
    assert [r["run"] for r in ca.read_booked(tmp_path)] == ["b1"]
    assert [r["run"] for r in ca.read_held(running, tmp_path, later)] == ["r2"]
    assert reply["headroom"]["vram_mib"] == 32768 - 6144


def test_booked_row_is_dropped_once_its_pid_is_gone(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.group_em import session_registry

    _booked(tmp_path)
    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: False)
    ca.admit("r2", "t", _sample(jobs=[{"run": "b1"}]), {"vram_mib": 1000}, home=tmp_path, now=NOW + ca.GRANT_TTL_S + 10)
    assert ca.read_booked(tmp_path) == []


def test_record_progress_updates_the_booked_row_and_refuses_an_unknown_run(tmp_path: Path) -> None:
    _booked(tmp_path)
    assert ca.record_progress("b1", 40.0, tmp_path, now=NOW + 5) == {"updated": True, "run": "b1"}
    row = ca.read_booked(tmp_path)[0]
    assert row["progress"] == 40.0 and row["progress_at"] == NOW + 5
    assert ca.record_progress("nope", 1.0, tmp_path)["reason"] == "no_grant"


def test_booked_row_with_a_reused_pid_is_dropped_and_a_matching_start_is_kept(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.ask_spool import record
    from coordinator_core.group_em import session_registry

    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: True)
    _booked(tmp_path, pid_started_at=1000.0)
    later = NOW + ca.GRANT_TTL_S + 10

    monkeypatch.setattr(record, "pid_create_time", lambda pid: 1000.5)
    ca.admit("r2", "t", _sample(jobs=[{"run": "b1"}]), {"vram_mib": 1000}, home=tmp_path, now=later)
    assert [r["run"] for r in ca.read_booked(tmp_path)] == ["b1"]

    monkeypatch.setattr(record, "pid_create_time", lambda pid: 5000.0)
    ca.admit("r3", "t", _sample(jobs=[{"run": "b1"}]), {"vram_mib": 1000}, home=tmp_path, now=later)
    assert ca.read_booked(tmp_path) == []
