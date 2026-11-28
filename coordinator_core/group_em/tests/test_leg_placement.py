"""Tests for coordinator_core.group_em.leg_placement; every case writes under tmp_path only."""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.group_em import box_registry, capacity_record, job_peaks, leg_placement

NOW = 1000.0


def _sample(load, os_name="Linux"):
    return {
        "os": os_name,
        "cores_logical": 16,
        "load_pct": load,
        "ram_avail_mb": 50_000,
        "gpus": [{"index": 0, "vram_total_mib": 32768, "vram_used_mib": 1000}],
        "jobs": [],
    }


def _fleet(tmp_path: Path, local_load, remotes: dict, stamp_at: float = NOW) -> Path:
    home = tmp_path / "home"
    directory = home / "state" / "group-em"
    assert capacity_record.stamp(_sample(local_load), home, now=stamp_at)
    for name, load in remotes.items():
        root = tmp_path / name
        box_registry.register(root, {}, box_id=name, directory=directory)
        if load != "absent":
            assert capacity_record.stamp({**_sample(load), "box_id": name}, root, now=stamp_at)
    job_peaks.record("test", {"vram_mib": 100, "ram_mb": 1000, "cores": 2}, home, now=NOW)
    return home


def test_least_loaded_remote_below_local_is_chosen(tmp_path: Path) -> None:
    home = _fleet(tmp_path, 80.0, {"gipsy": 10.0, "mini": 30.0})
    res = leg_placement.choose_leg_box("test", home=home, now=NOW + 10)
    assert res["box_id"] == "gipsy" and res["local"] is False
    assert res["box"]["box_id"] == "gipsy"
    assert res["load_pct"] == 10.0 and res["local_load_pct"] == 80.0


def test_local_wins_when_it_is_least_loaded(tmp_path: Path) -> None:
    home = _fleet(tmp_path, 5.0, {"gipsy": 10.0})
    res = leg_placement.choose_leg_box("test", home=home, now=NOW + 10)
    assert res["local"] is True and res["box"] is None
    assert res["load_pct"] == 5.0 and res["local_load_pct"] == 5.0


def test_equal_load_stays_local(tmp_path: Path) -> None:
    home = _fleet(tmp_path, 40.0, {"gipsy": 40.0})
    res = leg_placement.choose_leg_box("test", home=home, now=NOW + 10)
    assert res["local"] is True and res["box"] is None


def test_stale_remote_is_never_chosen(tmp_path: Path) -> None:
    home = _fleet(tmp_path, 80.0, {"gipsy": 1.0})
    later = NOW + capacity_record.TTL_S + 5
    capacity_record.stamp(_sample(80.0), home, now=later - 1)
    res = leg_placement.choose_leg_box("test", home=home, now=later)
    assert res["local"] is True and res["reason"] == "no_remote_below_local"


def test_unsampled_remote_and_unmeasured_load_are_skipped(tmp_path: Path) -> None:
    home = _fleet(tmp_path, 80.0, {"ghost": "absent", "blind": None})
    res = leg_placement.choose_leg_box("test", home=home, now=NOW + 10)
    assert res["local"] is True and res["local_load_pct"] == 80.0


def test_local_load_unmeasured_holds_local(tmp_path: Path) -> None:
    home = _fleet(tmp_path, None, {"gipsy": 1.0})
    res = leg_placement.choose_leg_box("test", home=home, now=NOW + 10)
    assert res["local"] is True and res["reason"] == "local_load_unmeasured"


def test_unmeasured_leg_type_has_no_remote_candidate(tmp_path: Path) -> None:
    home = _fleet(tmp_path, 80.0, {"gipsy": 1.0})
    res = leg_placement.choose_leg_box("never-measured", home=home, now=NOW + 10)
    assert res["local"] is True


def test_empty_home_and_unreadable_registry_never_raise(tmp_path: Path) -> None:
    res = leg_placement.choose_leg_box("test", home=tmp_path / "nowhere", now=NOW)
    assert res["local"] is True and res["box"] is None
    assert set(res) == {"box", "box_id", "local", "load_pct", "local_load_pct", "reason"}


def test_choosing_spawns_nothing(tmp_path: Path, monkeypatch) -> None:
    home = _fleet(tmp_path, 80.0, {"gipsy": 10.0})

    def _boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert leg_placement.choose_leg_box("test", home=home, now=NOW + 10)["box_id"] == "gipsy"
