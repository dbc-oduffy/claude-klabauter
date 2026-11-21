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
