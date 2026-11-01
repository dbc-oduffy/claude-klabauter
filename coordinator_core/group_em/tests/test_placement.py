"""Tests for coordinator_core.group_em.placement; every case writes under tmp_path only."""

from __future__ import annotations

from coordinator_core.group_em import box_registry, placement


def _two_boxes(tmp_path):
    primary = tmp_path / "primary" / "state" / "group-em"
    small = tmp_path / "small"
    big = tmp_path / "big"
    a = box_registry.register(small, {"os": "linux", "ram_mb": 8000}, directory=primary)
    b = box_registry.register(big, {"os": "linux", "ram_mb": 64000}, directory=primary)
    return primary, small, big, a, b


def test_picks_satisfying_box_and_intake_returns_it(tmp_path):
    primary, small, big, a, b = _two_boxes(tmp_path)
    res = placement.place_run("run/1", {"min_ram_mb": 32000, "os": "linux"}, brief="go", directory=primary)
    assert res["ok"] and res["placement"]["box_id"] == b["box_id"]
    intake = placement.read_intake(big)
    assert [p["run"] for p in intake] == ["run/1"]
    assert intake[0]["brief"] == "go" and intake[0]["requirement"]["min_ram_mb"] == 32000
    assert placement.read_intake(small) == []
    assert (big / "state" / "group-em" / "box" / "placements" / "run_1.json").is_file()


def test_first_satisfying_in_id_order(tmp_path):
    primary, small, big, a, b = _two_boxes(tmp_path)
    res = placement.place_run("r", {"os": "linux"}, directory=primary)
    assert res["placement"]["box_id"] == min(a["box_id"], b["box_id"])


def test_explicit_box_wins_but_must_satisfy(tmp_path):
    primary, small, big, a, b = _two_boxes(tmp_path)
    ok = placement.place_run("r", {"min_ram_mb": 1}, box_id=a["box_id"], directory=primary)
    assert ok["ok"] and ok["placement"]["box_id"] == a["box_id"]
    bad = placement.place_run("r2", {"min_ram_mb": 32000}, box_id=a["box_id"], directory=primary)
    assert bad == {"ok": False, "reason": "unsatisfied", "box_id": a["box_id"]}
    assert placement.read_intake(small)[0]["run"] == "r"


def test_refusals_never_raise(tmp_path):
    primary, small, big, a, b = _two_boxes(tmp_path)
    assert placement.place_run("r", {"os": "darwin"}, directory=primary)["reason"] == "unsatisfied"
    assert placement.place_run("r", {}, box_id="nope", directory=primary)["reason"] == "no_box"
    empty = placement.place_run("r", {}, directory=tmp_path / "empty")
    assert empty == {"ok": False, "reason": "no_box"}
    assert placement.place_run("r", {"min_ram_mb": "x"}, directory=primary)["reason"] == "unsatisfied"


def test_read_intake_skips_malformed_and_missing(tmp_path):
    assert placement.read_intake(tmp_path) == []
    d = tmp_path / "state" / "group-em" / "box" / "placements"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("garbage", encoding="utf-8")
    (d / "list.json").write_text("[]", encoding="utf-8")
    assert placement.read_intake(tmp_path) == []


def test_distinct_runs_sharing_a_safe_filename_refuse_and_keep_the_first(tmp_path):
    primary, small, big, a, b = _two_boxes(tmp_path)
    first = placement.place_run("run/1", {"os": "linux"}, brief="first", directory=primary)
    assert first["ok"]
    second = placement.place_run("run_1", {"os": "linux"}, brief="second", directory=primary)
    assert second == {"ok": False, "reason": "run_collision", "box_id": first["placement"]["box_id"]}
    again = placement.place_run("run/1", {"os": "linux"}, brief="third", directory=primary)
    assert again["ok"]
    box_root = small if first["placement"]["box_id"] == a["box_id"] else big
    assert [p["brief"] for p in placement.read_intake(box_root)] == ["third"]
