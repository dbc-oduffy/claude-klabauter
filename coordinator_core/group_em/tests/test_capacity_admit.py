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
