"""Tests for coordinator_core.group_em.placement; every case writes under tmp_path only."""

from __future__ import annotations

from coordinator_core.group_em import box_registry, capacity_admit, capacity_record, job_peaks, placement
from pathlib import Path


def _two_boxes(tmp_path):
    primary = tmp_path / "primary" / "state" / "group-em"
    small = tmp_path / "small"
    big = tmp_path / "big"
    a = box_registry.register(small, {"os": "linux", "ram_mb": 8000}, directory=primary)
    b = box_registry.register(big, {"os": "linux", "ram_mb": 64000}, directory=primary)
    return primary, small, big, a, b


def test_picks_satisfying_box_and_intake_returns_it(tmp_path: Path) -> None:
    primary, small, big, a, b = _two_boxes(tmp_path)
    res = placement.place_run("run/1", {"min_ram_mb": 32000, "os": "linux"}, brief="go", directory=primary)
    assert res["ok"] and res["placement"]["box_id"] == b["box_id"]
    intake = placement.read_intake(big)
    assert [p["run"] for p in intake] == ["run/1"]
    assert intake[0]["brief"] == "go" and intake[0]["requirement"]["min_ram_mb"] == 32000
    assert placement.read_intake(small) == []
    assert (big / "state" / "group-em" / "box" / "placements" / "run_1.json").is_file()


def test_first_satisfying_in_id_order(tmp_path: Path) -> None:
    primary, small, big, a, b = _two_boxes(tmp_path)
    res = placement.place_run("r", {"os": "linux"}, directory=primary)
    assert res["placement"]["box_id"] == min(a["box_id"], b["box_id"])


def test_explicit_box_wins_but_must_satisfy(tmp_path: Path) -> None:
    primary, small, big, a, b = _two_boxes(tmp_path)
    ok = placement.place_run("r", {"min_ram_mb": 1}, box_id=a["box_id"], directory=primary)
    assert ok["ok"] and ok["placement"]["box_id"] == a["box_id"]
    bad = placement.place_run("r2", {"min_ram_mb": 32000}, box_id=a["box_id"], directory=primary)
    assert bad == {"ok": False, "reason": "unsatisfied", "box_id": a["box_id"]}
    assert placement.read_intake(small)[0]["run"] == "r"


def test_refusals_never_raise(tmp_path: Path) -> None:
    primary, small, big, a, b = _two_boxes(tmp_path)
    assert placement.place_run("r", {"os": "darwin"}, directory=primary)["reason"] == "unsatisfied"
    assert placement.place_run("r", {}, box_id="nope", directory=primary)["reason"] == "no_box"
    empty = placement.place_run("r", {}, directory=tmp_path / "empty")
    assert empty == {"ok": False, "reason": "no_box"}
    assert placement.place_run("r", {"min_ram_mb": "x"}, directory=primary)["reason"] == "unsatisfied"


def test_read_intake_skips_malformed_and_missing(tmp_path: Path) -> None:
    assert placement.read_intake(tmp_path) == []
    d = tmp_path / "state" / "group-em" / "box" / "placements"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("garbage", encoding="utf-8")
    (d / "list.json").write_text("[]", encoding="utf-8")
    assert placement.read_intake(tmp_path) == []


def test_distinct_runs_sharing_a_safe_filename_refuse_and_keep_the_first(tmp_path: Path) -> None:
    primary, small, big, a, b = _two_boxes(tmp_path)
    first = placement.place_run("run/1", {"os": "linux"}, brief="first", directory=primary)
    assert first["ok"]
    second = placement.place_run("run_1", {"os": "linux"}, brief="second", directory=primary)
    assert second == {"ok": False, "reason": "run_collision", "box_id": first["placement"]["box_id"]}
    again = placement.place_run("run/1", {"os": "linux"}, brief="third", directory=primary)
    assert again["ok"]
    box_root = small if first["placement"]["box_id"] == a["box_id"] else big
    assert [p["brief"] for p in placement.read_intake(box_root)] == ["third"]


def test_os_requirement_matches_by_list_membership_case_insensitively() -> None:
    cap = {"os": "Windows"}
    assert placement.satisfies(cap, {"os": ["linux", "windows"]})
    assert placement.satisfies(cap, {"os": "windows"})
    assert not placement.satisfies(cap, {"os": ["linux", "darwin"]})
    assert not placement.satisfies({}, {"os": ["linux"]})


def test_min_free_floor_reads_measured_free_headroom_off_the_sample() -> None:
    sample = _sample("Windows", vram_used=6144)
    cap = placement.sample_capability(sample)
    assert cap["free_vram_mib"] == 32768 - 6144
    assert placement.satisfies(cap, {"min_free_vram_mib": 20000})
    assert not placement.satisfies(cap, {"min_free_vram_mib": 30000})
    assert not placement.satisfies(placement.sample_capability({}), {"min_free_ram_mb": 1})


def _sample(os_name, vram_used=6144, ram_avail=50_000, load=10.0):
    return {
        "os": os_name,
        "cores_logical": 16,
        "load_pct": load,
        "ram_avail_mb": ram_avail,
        "gpus": [{"index": 0, "vram_total_mib": 32768, "vram_used_mib": vram_used}],
        "jobs": [],
    }


def _fleet(tmp_path, now=1000.0):
    primary = tmp_path / "primary" / "state" / "group-em"
    boxes = {}
    for name, os_name, used, ram in (
        ("win", "Windows", 6144, 50_000),
        ("lin", "Linux", 1000, 90_000),
        ("mac", "Darwin", 30000, 8_000),
    ):
        root = tmp_path / name
        rec = box_registry.register(root, {}, box_id=name, directory=primary)
        assert capacity_record.stamp({**_sample(os_name, used, ram), "box_id": name}, root, now=now)
        boxes[name] = rec
    job_peaks.record("render", {"vram_mib": 20000, "ram_mb": 8000, "cores": 2}, tmp_path / "primary", now=now)
    return primary, tmp_path / "primary"


def test_candidates_lists_capable_boxes_sorted_by_headroom_and_filters_by_platform(tmp_path: Path) -> None:
    primary, home = _fleet(tmp_path)
    rows = placement.candidates("render", directory=primary, home=home, now=1010.0)
    assert [r["box_id"] for r in rows] == ["lin", "win"]
    assert rows[0]["headroom"]["vram_mib"] == 32768 - 1000 and rows[0]["peak_basis"] == "measured"
    only_win = placement.candidates("render", os=["windows"], directory=primary, home=home, now=1010.0)
    assert [r["box_id"] for r in only_win] == ["win"]
    assert placement.candidates("render", os="darwin", directory=primary, home=home, now=1010.0) == []


def test_candidates_subtracts_granted_but_not_started_jobs(tmp_path: Path) -> None:
    primary, home = _fleet(tmp_path)
    admitted = capacity_admit.admit(
        "r1", "render", capacity_record.read({"box_id": "lin", "root": tmp_path / "lin"}, 1010.0),
        job_peaks.peak("render", home), home=tmp_path / "lin", now=1010.0,
    )
    assert admitted["grant"]
    assert placement.candidates("render", os="linux", directory=primary, home=home, now=1011.0) == []
    job_peaks.record("small", {"vram_mib": 5000}, home, now=1011.0)
    small = placement.candidates("small", os="linux", directory=primary, home=home, now=1011.0)
    assert [r["headroom"]["vram_mib"] for r in small] == [32768 - 1000 - 20000]
    floor = placement.candidates("small", requirement={"min_free_vram_mib": 20000}, directory=primary, home=home, now=1011.0)
    assert [r["box_id"] for r in floor] == ["win"]


def test_candidates_skips_stale_boxes_and_unmeasured_job_types(tmp_path: Path) -> None:
    primary, home = _fleet(tmp_path)
    assert placement.candidates("render", directory=primary, home=home, now=1000.0 + capacity_record.TTL_S + 1) == []
    assert placement.candidates("never-measured", directory=primary, home=home, now=1010.0) == []
    declared = placement.candidates("never-measured", declared={"ram_mb": 100}, directory=primary, home=home, now=1010.0)
    assert {r["peak_basis"] for r in declared} == {"declared"} and len(declared) == 3


def test_candidates_never_raises_on_an_empty_registry(tmp_path: Path) -> None:
    assert placement.candidates("render", directory=tmp_path / "nowhere", home=tmp_path / "nohome") == []
