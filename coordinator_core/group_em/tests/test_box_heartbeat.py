"""box_heartbeat: stamp on box B reads live from box A; not-live past TTL, absent or malformed."""

from coordinator_core.group_em import box_heartbeat, box_registry


def _box_b(tmp_path):
    home = tmp_path / "boxb"
    primary = tmp_path / "primary" / "state" / "group-em"
    row = box_registry.register(home, {"os": "linux"}, directory=primary)
    return home, row


def test_stamp_on_b_read_live_from_a(tmp_path):
    home, row = _box_b(tmp_path)
    assert box_heartbeat.stamp(home, now=1000.0)
    got = box_heartbeat.read(row, now=1030.0)
    assert got == {"box_id": row["box_id"], "live": True, "age_s": 30.0}


def test_not_live_past_ttl_and_live_at_boundary(tmp_path):
    home, row = _box_b(tmp_path)
    box_heartbeat.stamp(home, now=1000.0)
    assert box_heartbeat.read(row, now=1000.0 + box_heartbeat.TTL_S)["live"] is True
    got = box_heartbeat.read(row, now=1000.0 + box_heartbeat.TTL_S + 1)
    assert got["live"] is False and got["age_s"] == box_heartbeat.TTL_S + 1


def test_absent_record_not_live(tmp_path):
    home, row = _box_b(tmp_path)
    assert box_heartbeat.read(row) == {"box_id": row["box_id"], "live": False, "age_s": None}


def test_malformed_record_not_live(tmp_path):
    home, row = _box_b(tmp_path)
    box_heartbeat.stamp(home, now=1.0)
    (home / "state" / "group-em" / "box" / "heartbeat.json").write_text("{nope", encoding="utf-8")
    assert box_heartbeat.read(row, now=2.0)["live"] is False


def test_future_ts_clamps_age(tmp_path):
    home, row = _box_b(tmp_path)
    box_heartbeat.stamp(home, now=5000.0)
    got = box_heartbeat.read(row, now=4990.0)
    assert got["live"] is True and got["age_s"] == 0.0


def test_stamp_records_box_id_and_second_box_isolated(tmp_path):
    home, row = _box_b(tmp_path)
    other = tmp_path / "boxc"
    row_c = box_registry.register(other, {}, directory=tmp_path / "primary" / "state" / "group-em")
    box_heartbeat.stamp(home, now=10.0)
    assert box_heartbeat.read(row, now=11.0)["live"] is True
    assert box_heartbeat.read(row_c, now=11.0)["live"] is False
