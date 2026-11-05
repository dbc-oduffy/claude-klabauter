"""box_probe: capability record shape, zero spawns, and the box.json merge."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.group_em import box_probe, box_registry


@pytest.fixture
def no_spawn(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("probe must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)


def test_probe_shape_and_zero_spawn(no_spawn):
    cap = box_probe.probe()
    assert set(cap) == {"os", "arch", "cpu_count", "ram_mb", "python", "gpu", "has_p4", "has_ue"}
    assert cap["gpu"] is None
    assert cap["os"] and cap["python"]
    assert isinstance(cap["cpu_count"], int) and cap["cpu_count"] > 0
    assert isinstance(cap["ram_mb"], int) and cap["ram_mb"] > 0


def test_probe_reports_tool_presence(no_spawn, monkeypatch, tmp_path):
    from coordinator_core.group_em import placement

    cap = box_probe.probe()
    assert isinstance(cap["has_p4"], bool) and isinstance(cap["has_ue"], bool)

    monkeypatch.setattr(box_probe.shutil, "which", lambda name: None)
    monkeypatch.delenv("UE_ROOT", raising=False)
    monkeypatch.delenv("UNREAL_ENGINE_ROOT", raising=False)
    absent = box_probe.probe()
    assert absent["has_p4"] is False and absent["has_ue"] is False
    assert placement.satisfies(absent, {"has_ue": True}) is False

    monkeypatch.setenv("UE_ROOT", str(tmp_path))
    monkeypatch.setattr(box_probe.shutil, "which", lambda name: "/bin/p4" if name == "p4" else None)
    present = box_probe.probe()
    assert present["has_ue"] is True and present["has_p4"] is True
    assert placement.satisfies(present, {"has_ue": True}) is True

    monkeypatch.setenv("UE_ROOT", str(tmp_path / "missing"))
    assert box_probe.probe()["has_ue"] is False

    monkeypatch.delenv("UE_ROOT")
    monkeypatch.setattr(box_probe.shutil, "which", lambda name: "/x" if name == "UnrealEditor-Cmd" else None)
    assert box_probe.probe()["has_ue"] is True


def test_ram_unknown_is_none(monkeypatch):
    monkeypatch.setattr(box_probe.os, "name", "posix")

    def _raise(_name):
        raise ValueError("no such conf")

    monkeypatch.setattr(box_probe.os, "sysconf", _raise, raising=False)
    assert box_probe._ram_mb() is None


def test_write_capability_mints_identity_and_records(tmp_path, no_spawn):
    rec = box_probe.write_capability(tmp_path)
    box_id = box_registry.local_box_id(tmp_path)
    assert box_id != box_registry.LOCAL_BOX_ID
    on_disk = json.loads((tmp_path / "state" / "group-em" / "box.json").read_text(encoding="utf-8"))
    assert on_disk["box_id"] == box_id
    assert on_disk["capability"] == rec["capability"]
    assert on_disk["capability"]["gpu"] is None


def test_write_capability_keeps_existing_id_and_refreshes(tmp_path):
    box_id = box_registry.ensure_identity(tmp_path)
    box_probe.write_capability(tmp_path)
    path = tmp_path / "state" / "group-em" / "box.json"
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["capability"] = {"os": "stale"}
    path.write_text(json.dumps(stale), encoding="utf-8")
    box_probe.write_capability(tmp_path)
    fresh = json.loads(path.read_text(encoding="utf-8"))
    assert fresh["box_id"] == box_id
    assert fresh["capability"]["os"] != "stale"
    assert "minted_at" in fresh


def test_stored_capability_reads_without_minting(tmp_path):
    assert box_probe.stored_capability(tmp_path) is None
    assert not (tmp_path / "state").exists()
    box_registry.ensure_identity(tmp_path)
    assert box_probe.stored_capability(tmp_path) is None
    rec = box_probe.write_capability(tmp_path)
    assert box_probe.stored_capability(tmp_path) == rec["capability"]


@pytest.mark.parametrize("body", ["[1, 2]", "not json", '{"capability": "x"}'])
def test_stored_capability_refuses_malformed(tmp_path, body):
    path = tmp_path / "state" / "group-em" / "box.json"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    assert box_probe.stored_capability(tmp_path) is None


def test_write_capability_survives_non_dict_box_json(tmp_path):
    path = tmp_path / "state" / "group-em" / "box.json"
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2]", encoding="utf-8")
    box_probe.write_capability(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["box_id"].startswith("box-")
    assert "capability" in data


def test_stored_capability_reads_recorded_and_refuses_absent_or_empty(tmp_path):
    assert box_probe.stored_capability(tmp_path) is None
    path = tmp_path / "state" / "group-em" / "box.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"capability": {}}), encoding="utf-8")
    assert box_probe.stored_capability(tmp_path) is None
    box_probe.write_capability(tmp_path)
    assert box_probe.stored_capability(tmp_path)["os"]
