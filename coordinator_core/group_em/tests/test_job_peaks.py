"""job_peaks: measured max per axis, sample count, never-measured reads None, no declared field."""

import json
from pathlib import Path

from coordinator_core.group_em import job_peaks


def _file(home: Path) -> Path:
    return home / "state" / "group-em" / "box" / "job-peaks.json"


def test_unmeasured_peak_is_none(tmp_path: Path) -> None:
    assert job_peaks.peak("render", tmp_path) is None


def test_record_then_peak(tmp_path: Path) -> None:
    assert job_peaks.record("render", {"ram_mb": 4000, "vram_mib": 9000, "cores": 4}, tmp_path, now=5.0)
    got = job_peaks.peak("render", tmp_path)
    assert got["ram_mb"] == 4000 and got["vram_mib"] == 9000 and got["cores"] == 4
    assert got["samples"] == 1 and got["measured_at"] == 5.0 and isinstance(got["box_id"], str)


def test_keeps_max_per_axis_and_counts_samples(tmp_path: Path) -> None:
    job_peaks.record("t", {"ram_mb": 100, "vram_mib": 900, "cores": 2}, tmp_path, now=1.0)
    job_peaks.record("t", {"ram_mb": 300, "vram_mib": 400, "cores": 1}, tmp_path, now=2.0)
    got = job_peaks.peak("t", tmp_path)
    assert (got["ram_mb"], got["vram_mib"], got["cores"]) == (300, 900, 2)
    assert got["samples"] == 2 and got["measured_at"] == 2.0


def test_partial_axes_leave_others_absent_not_zero(tmp_path: Path) -> None:
    job_peaks.record("cpu-only", {"ram_mb": 50}, tmp_path)
    got = job_peaks.peak("cpu-only", tmp_path)
    assert got["ram_mb"] == 50 and "vram_mib" not in got and "cores" not in got
    job_peaks.record("cpu-only", {"vram_mib": 7}, tmp_path)
    got = job_peaks.peak("cpu-only", tmp_path)
    assert got["ram_mb"] == 50 and got["vram_mib"] == 7 and got["samples"] == 2


def test_unusable_measurement_records_nothing(tmp_path: Path) -> None:
    for bad in ({}, {"ram_mb": None}, {"ram_mb": -1}, {"ram_mb": "9"}, {"ram_mb": True}, {"declared": 5}):
        assert job_peaks.record("t", bad, tmp_path) is False
    assert job_peaks.record("", {"ram_mb": 1}, tmp_path) is False
    assert job_peaks.record("t", "x", tmp_path) is False
    assert job_peaks.peak("t", tmp_path) is None


def test_no_declared_field_stored(tmp_path: Path) -> None:
    job_peaks.record("t", {"ram_mb": 1, "declared": 99, "declared_ram_gib": 8}, tmp_path)
    assert set(job_peaks.peak("t", tmp_path)) == {"ram_mb", "samples", "measured_at", "box_id"}


def test_job_types_isolated(tmp_path: Path) -> None:
    job_peaks.record("a", {"ram_mb": 1}, tmp_path)
    job_peaks.record("b", {"ram_mb": 2}, tmp_path)
    assert job_peaks.peak("a", tmp_path)["ram_mb"] == 1 and job_peaks.peak("b", tmp_path)["ram_mb"] == 2


def test_malformed_store_reads_none_and_recovers(tmp_path: Path) -> None:
    _file(tmp_path).parent.mkdir(parents=True)
    _file(tmp_path).write_text("{nope", encoding="utf-8")
    assert job_peaks.peak("t", tmp_path) is None
    assert job_peaks.record("t", {"ram_mb": 3}, tmp_path)
    assert json.loads(_file(tmp_path).read_text(encoding="utf-8"))["t"]["ram_mb"] == 3


def test_record_false_on_io_failure(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert job_peaks.record("t", {"ram_mb": 1}, blocker) is False
