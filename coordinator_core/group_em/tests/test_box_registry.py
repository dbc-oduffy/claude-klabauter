"""Tests for coordinator_core.group_em.box_registry; every case writes under tmp_path only."""

from __future__ import annotations

import json

from coordinator_core.group_em import box_registry


def test_local_box_id_absent_is_local(tmp_path):
    assert box_registry.local_box_id(tmp_path) == "local"


def test_local_box_id_malformed_is_local(tmp_path):
    p = tmp_path / "state" / "group-em"
    p.mkdir(parents=True)
    (p / "box.json").write_text("{not json", encoding="utf-8")
    assert box_registry.local_box_id(tmp_path) == "local"
    (p / "box.json").write_text(json.dumps({"box_id": "../evil"}), encoding="utf-8")
    assert box_registry.local_box_id(tmp_path) == "local"


def test_ensure_identity_mints_once(tmp_path):
    first = box_registry.ensure_identity(tmp_path)
    assert first.startswith("box-") and first != "local"
    assert box_registry.ensure_identity(tmp_path) == first
    assert box_registry.local_box_id(tmp_path) == first


def test_register_round_trip_second_home(tmp_path):
    primary = tmp_path / "primary" / "state" / "group-em"
    second = tmp_path / "second"
    cap = {"os": "linux", "gpu": "5090"}
    rec = box_registry.register(second, cap, directory=primary)
    assert rec["kind"] == "group-em-box" and rec["schema_version"] == 1
    assert rec["box_id"] == box_registry.local_box_id(second)
    assert (primary / "boxes" / (rec["box_id"] + ".json")).is_file()
    boxes = box_registry.read_boxes(directory=primary)
    assert len(boxes) == 1
    assert boxes[0]["box_id"] == rec["box_id"]
    assert boxes[0]["root"] == str(second)
    assert boxes[0]["capability"] == cap
    assert box_registry.box_root(rec["box_id"], directory=primary) == second


def test_register_peer_by_explicit_id_writes_nothing_under_root(tmp_path):
    d = tmp_path / "me"
    root = tmp_path / "far"
    t = {"kind": "ssh", "host": "10.0.0.2", "invoke": "/x/ci"}
    rec = box_registry.register(root, {"os": "mac"}, directory=d, box_id="box-far", transport=t)
    assert rec["box_id"] == "box-far" and rec["transport"] == t
    assert not root.exists()
    assert box_registry.read_boxes(directory=d)[0]["transport"] == t
    assert "transport" not in box_registry.register(tmp_path / "s", {}, directory=d)


def test_register_rejects_unsafe_explicit_id(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        box_registry.register(tmp_path / "r", {}, directory=tmp_path, box_id="../evil")
    assert box_registry.read_boxes(directory=tmp_path) == []


def test_reregister_keeps_id_and_refreshes_capability(tmp_path):
    primary = tmp_path / "p"
    second = tmp_path / "s"
    a = box_registry.register(second, {"cores": 8}, directory=primary)
    b = box_registry.register(second, {"cores": 16}, directory=primary)
    assert a["box_id"] == b["box_id"]
    boxes = box_registry.read_boxes(directory=primary)
    assert [x["capability"] for x in boxes] == [{"cores": 16}]


def test_read_boxes_skips_malformed_and_missing_dir(tmp_path):
    assert box_registry.read_boxes(directory=tmp_path) == []
    good = box_registry.register(tmp_path / "s", {}, directory=tmp_path)
    d = tmp_path / "boxes"
    (d / "bad.json").write_text("garbage", encoding="utf-8")
    (d / "wrongkind.json").write_text(json.dumps({"kind": "x", "box_id": "x"}), encoding="utf-8")
    (d / "nocap.json").write_text(
        json.dumps({"kind": "group-em-box", "box_id": "nocap", "root": "r"}), encoding="utf-8"
    )
    assert [b["box_id"] for b in box_registry.read_boxes(directory=tmp_path)] == [good["box_id"]]


def test_box_root_unknown_and_unsafe(tmp_path):
    assert box_registry.box_root("nope", directory=tmp_path) is None
    assert box_registry.box_root("../x", directory=tmp_path) is None
    assert box_registry.box_root("", directory=tmp_path) is None
