"""The box-status line never reports this box, or a far box registered at this box's home, as unreachable.

Machine-a 2026-11-23: box.json read `box-931c698e`, the registry carried `machine-a` and `Mac-mini` rows both
rooted at Machine-a's own settings home, and nothing in production stamped a heartbeat, so the watch
printed "box machine-a: unreachable" and "box Mac-mini: unreachable" for a live fleet.
"""
from __future__ import annotations

from coordinator_core.group_em import box_heartbeat, box_registry
from coordinator_core.group_em import watch


def _boxes(monkeypatch, tmp_path, rows):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: rows)
    monkeypatch.setattr(box_registry, "local_box_id", lambda home=None: "box-931c698e")
    monkeypatch.setattr("platform.node", lambda: "Machine-a")


def test_this_box_under_its_hostname_is_not_listed(monkeypatch, tmp_path):
    _boxes(monkeypatch, tmp_path, [{"box_id": "machine-a", "root": str(tmp_path / "home")}])
    assert watch.remote_box_rows() == []


def test_a_far_name_rooted_at_this_home_is_flagged_misregistered(monkeypatch, tmp_path):
    _boxes(monkeypatch, tmp_path, [{"box_id": "Mac-mini", "root": str(tmp_path / "home")}])
    rows = watch.remote_box_rows()
    assert [r["box_id"] for r in rows] == ["Mac-mini"] and rows[0]["misregistered"] is True
    census = {"repo_subscribed": 0, "repo_live": 0, "live": 0, "other_repos": {}, "unwatched": 0,
              "remote_boxes": rows}
    line = watch.box_line(census)
    assert "box Mac-mini: MISREGISTERED" in line and "unreachable" not in line


def test_a_far_box_with_a_fresh_heartbeat_reads_live(monkeypatch, tmp_path):
    far = tmp_path / "far"
    _boxes(monkeypatch, tmp_path, [{"box_id": "Machine-b", "root": str(far)}])
    assert box_heartbeat.stamp(far) is True
    (row,) = watch.remote_box_rows()
    assert row["reachable"] is True and row["live"] is True


def test_the_tick_stamps_this_boxes_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    watch._stamp_box_heartbeat()
    assert box_heartbeat.read({"box_id": "x", "root": str(tmp_path / "home")})["live"] is True
