"""capacity_record: stamp on box B reads from box A with age_s/stale; absent, malformed, skewed."""

import json
from pathlib import Path

from coordinator_core.group_em import capacity_record, box_registry

GPU = {"index": 0, "name": "RTX 5090", "vram_total_mib": 32768, "vram_used_mib": 6144, "util_pct": 1}
SAMPLE = {
    "os": "Windows",
    "arch": "AMD64",
    "cores_physical": 24,
    "cores_logical": 24,
    "load_pct": 12.5,
    "ram_total_mb": 131072,
    "ram_avail_mb": 57000,
    "gpus": [GPU],
    "jobs": [{"run": "r1", "job_type": "t", "session_id": "s", "peak_ram_mb": 100, "peak_vram_mib": 200, "cores": 2}],
}


def _box_b(tmp_path):
    home = tmp_path / "boxb"
    primary = tmp_path / "primary" / "state" / "group-em"
    return home, box_registry.register(home, {"os": "linux"}, directory=primary)


def _file(home: Path) -> Path:
    return home / "state" / "group-em" / "box" / "capacity.json"


def test_stamp_then_read_from_peer_row(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    assert capacity_record.stamp(SAMPLE, home, now=1000.0)
    got = capacity_record.read(row, now=1030.0)
    assert got["box_id"] == row["box_id"] and got["age_s"] == 30.0 and got["stale"] is False
    assert got["schema_version"] == capacity_record.SCHEMA_VERSION
    assert got["gpus"] == [GPU] and got["ram_avail_mb"] == 57000 and got["jobs"] == SAMPLE["jobs"]
    assert "gpu_unavailable" not in got


def test_stale_past_ttl_and_fresh_at_boundary(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    capacity_record.stamp(SAMPLE, home, now=1000.0)
    assert capacity_record.read(row, now=1000.0 + capacity_record.TTL_S)["stale"] is False
    got = capacity_record.read(row, now=1000.0 + capacity_record.TTL_S + 1)
    assert got["stale"] is True and got["age_s"] == capacity_record.TTL_S + 1
    assert got["ram_avail_mb"] == 57000


def test_peer_row_read_hits_root_path_and_reports_stale_past_ttl(tmp_path: Path, monkeypatch) -> None:
    home, row = _box_b(tmp_path)
    assert Path(row["root"]) == home
    assert capacity_record.stamp(SAMPLE, home, now=1000.0)
    expected = Path(row["root"]) / "state" / "group-em" / "box" / "capacity.json"
    assert expected.is_file()

    opened: list = []
    real_open = open

    def spy(path, *a, **k):
        opened.append(Path(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    got = capacity_record.read(row, now=1000.0 + capacity_record.TTL_S + 1)
    monkeypatch.undo()

    assert opened == [expected]
    assert got["stale"] is True and got["age_s"] > capacity_record.TTL_S


def test_ttl_covers_slowest_watch_tick() -> None:
    from coordinator_core.group_em import watch

    assert capacity_record.WATCH_TICK_CEILING_S == watch._POLL_INTERVAL_CEILING_SECONDS
    assert capacity_record.TTL_S > capacity_record.WATCH_TICK_CEILING_S


def test_absent_record_is_stale_without_zeroes(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    assert capacity_record.read(row) == {"box_id": row["box_id"], "stale": True, "age_s": None}


def test_malformed_record_is_stale(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    capacity_record.stamp(SAMPLE, home, now=1.0)
    _file(home).write_text("{nope", encoding="utf-8")
    assert capacity_record.read(row, now=2.0)["stale"] is True
    _file(home).write_text("[1]", encoding="utf-8")
    assert capacity_record.read(row, now=2.0)["age_s"] is None


def test_read_never_raises_on_bad_box() -> None:
    assert capacity_record.read({})["stale"] is True
    assert capacity_record.read(None)["stale"] is True


def test_future_ts_clamps_age(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    capacity_record.stamp(SAMPLE, home, now=5000.0)
    got = capacity_record.read(row, now=4990.0)
    assert got["age_s"] == 0.0 and got["stale"] is False


def test_gpu_unavailable_reason_kept_and_defaulted(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    capacity_record.stamp({"gpu_unavailable": "vendor-unsupported"}, home, now=1.0)
    got = capacity_record.read(row, now=1.0)
    assert got["gpu_unavailable"] == "vendor-unsupported" and "gpus" not in got
    capacity_record.stamp({}, home, now=1.0)
    assert capacity_record.read(row, now=1.0)["gpu_unavailable"] == capacity_record.GPU_NOT_SAMPLED


def test_stamp_drops_unknown_keys_and_is_valid_json(tmp_path: Path) -> None:
    home, _ = _box_b(tmp_path)
    capacity_record.stamp({"bogus": 1, "gpus": [dict(GPU, extra=9)]}, home, now=1.0)
    data = json.loads(_file(home).read_text(encoding="utf-8"))
    assert "bogus" not in data and "extra" not in data["gpus"][0]


def test_stamp_returns_false_on_io_failure(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert capacity_record.stamp(SAMPLE, blocker, now=1.0) is False


def test_second_box_isolated(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    row_c = box_registry.register(tmp_path / "boxc", {}, directory=tmp_path / "primary" / "state" / "group-em")
    capacity_record.stamp(SAMPLE, home, now=10.0)
    assert capacity_record.read(row, now=11.0)["stale"] is False
    assert capacity_record.read(row_c, now=11.0)["age_s"] is None


def _fleet(tmp_path: Path):
    """A local home plus one peer registered under it; returns (home, local_id, peer_home, peer_id)."""
    home = tmp_path / "local"
    local = box_registry.ensure_identity(home)
    peer_home = tmp_path / "peer"
    peer_id = box_registry.ensure_identity(peer_home)
    box_registry.register(peer_home, {}, box_id=peer_id, directory=home / "state" / "group-em")
    return home, local, peer_home, peer_id


def test_stamp_carries_saturated_and_top_cpu_as_pid_comm_pct(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    top = [{"pid": 7, "comm": "find", "pcpu": 99.0}, {"pid": 8, "comm": "x", "pct": 3.0, "extra": 1}]
    capacity_record.stamp(dict(SAMPLE, saturated=True, top_cpu=top), home, now=1.0)
    got = capacity_record.read(row, now=1.0)
    assert got["saturated"] is True
    assert got["top_cpu"] == [{"pid": 7, "comm": "find", "pct": 99.0}, {"pid": 8, "comm": "x", "pct": 3.0}]


def test_stamp_memory_merges_and_keeps_the_cpu_fields_and_ts(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    capacity_record.stamp(dict(SAMPLE, saturated=True), home, now=1000.0)
    assert capacity_record.stamp_memory(True, 2, home, now=1500.0)
    got = capacity_record.read(row, now=1500.0)
    assert (got["mem_critical"], got["paused"], got["mem_ts"]) == (True, 2, 1500.0)
    assert got["ts"] == 1000.0 and got["saturated"] is True and got["gpus"] == [GPU]


def test_stamp_keeps_the_governors_verdict(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    capacity_record.stamp_memory(True, 1, home, now=1000.0)
    capacity_record.stamp(SAMPLE, home, now=1010.0)
    got = capacity_record.read(row, now=1010.0)
    assert (got["mem_critical"], got["paused"], got["mem_ts"]) == (True, 1, 1000.0)
    assert got["ram_avail_mb"] == 57000


def test_memory_only_record_reads_stale_for_capacity_consumers(tmp_path: Path) -> None:
    home, row = _box_b(tmp_path)
    assert capacity_record.stamp_memory(True, 1, home, now=1000.0)
    got = capacity_record.read(row, now=1000.0)
    assert got["stale"] is True and got["mem_critical"] is True


def test_stamp_memory_returns_false_on_io_failure(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert capacity_record.stamp_memory(True, 0, blocker) is False


def test_read_refuses_a_root_that_is_not_absolute_here(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    capacity_record.stamp(SAMPLE, tmp_path / "rel", now=1.0)
    assert capacity_record.read({"box_id": "far", "root": "rel"}, now=1.0) == {
        "box_id": "far", "stale": True, "age_s": None}


def test_saturated_boxes_reads_a_fresh_remote_stamp_without_ssh(tmp_path: Path) -> None:
    home, local, peer_home, peer_id = _fleet(tmp_path)
    top = [{"pid": 7, "comm": "find", "pcpu": 99.0}]
    capacity_record.stamp(dict(SAMPLE, saturated=True, top_cpu=top), peer_home, now=1000.0)
    capacity_record.stamp_memory(True, 3, peer_home, now=1000.0)
    capacity_record.stamp(dict(SAMPLE, saturated=True), home, now=1000.0)  # this box: never listed
    rows = capacity_record.saturated_boxes(home, local, now=1010.0)
    assert [r["box_id"] for r in rows] == [peer_id]
    got = rows[0]
    assert got["stale"] is False and got["saturated"] is True and got["mem_critical"] is True
    assert got["paused"] == 3 and got["top_cpu"] == [{"pid": 7, "comm": "find", "pct": 99.0}]


def test_saturated_boxes_a_stale_record_is_not_saturated(tmp_path: Path) -> None:
    home, local, peer_home, peer_id = _fleet(tmp_path)
    capacity_record.stamp(dict(SAMPLE, saturated=True), peer_home, now=1000.0)
    capacity_record.stamp_memory(True, 3, peer_home, now=1000.0)
    late = 1000.0 + capacity_record.TTL_S + 1
    got = capacity_record.saturated_boxes(home, local, now=late)[0]
    assert got["stale"] is True and got["saturated"] is False and got["mem_critical"] is False
    assert got["paused"] == 0 and got["top_cpu"] == []


def test_saturated_boxes_ages_cpu_and_memory_independently(tmp_path: Path) -> None:
    home, local, peer_home, _ = _fleet(tmp_path)
    capacity_record.stamp(dict(SAMPLE, saturated=True), peer_home, now=1000.0)
    capacity_record.stamp_memory(True, 1, peer_home, now=1000.0 + capacity_record.TTL_S)
    got = capacity_record.saturated_boxes(home, local, now=1000.0 + capacity_record.TTL_S + 1)[0]
    assert got["saturated"] is False and got["mem_critical"] is True


def test_saturated_boxes_reads_a_far_box_from_the_live_snapshot(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.group_em import fleet_live

    home = tmp_path / "local"
    local = box_registry.ensure_identity(home)
    box_registry.register("far/root", {}, box_id="Mac-mini",
                          transport={"kind": "ssh", "host": "u@Mac-mini.local"},
                          directory=home / "state" / "group-em")
    rows = [{"box": "Mac-mini", "alias": "mini", "state": "ok", "stale": False, "saturated": True,
             "mem_critical": True, "paused": 2, "top_cpu": [{"pid": 4, "comm": "find", "pcpu": 88.0}]}]
    monkeypatch.setattr(fleet_live, "read_live_snapshot", lambda *a, **k: rows)
    got = capacity_record.saturated_boxes(home, local, now=1.0)
    assert [r["box_id"] for r in got] == ["Mac-mini"]
    assert got[0]["alias"] == "mini" and got[0]["saturated"] is True and got[0]["paused"] == 2
    assert got[0]["top_cpu"] == [{"pid": 4, "comm": "find", "pct": 88.0}]
    rows[0]["stale"] = True
    stale = capacity_record.saturated_boxes(home, local, now=1.0)[0]
    assert stale["stale"] is True and stale["saturated"] is False and stale["mem_critical"] is False
    rows.clear()
    assert capacity_record.saturated_boxes(home, local, now=1.0)[0]["stale"] is True


def test_local_verdicts_reads_this_boxes_own_stamp(tmp_path: Path) -> None:
    home = tmp_path / "me"
    capacity_record.stamp(dict(SAMPLE, saturated=True), home, now=1000.0)
    capacity_record.stamp_memory(True, 2, home, now=1000.0)
    got = capacity_record.local_verdicts(home, now=1010.0)
    assert (got["saturated"], got["mem_critical"], got["paused"]) == (True, True, 2)
    assert capacity_record.local_verdicts(tmp_path / "nobody", now=1.0)["saturated"] is False
