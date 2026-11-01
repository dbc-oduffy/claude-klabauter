"""Loopback end-to-end: a second settings-home registers, takes a placed run, and shows in A's roster.

Primary A and loopback B are two tmp settings-homes switched by COORDINATOR_SETTINGS_HOME. The flow
goes through the default ops on A, stamps B's records under B, and reads the roster back under A.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.group_em import admission_post, box_heartbeat, box_registry, watch
from coordinator_core.ops import group_em_box_federation as op


@pytest.fixture()
def loopback(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = tmp_path / "claude-config"
    (cfg / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))

    def _no_spawn(*args, **kwargs):
        raise AssertionError("subprocess spawn during the multi-box flow")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _no_spawn)
    monkeypatch.setattr(os, "system", _no_spawn)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(a))
    return a, b, monkeypatch


def test_run_placed_on_loopback_box_reaches_primary_roster(loopback):
    a, b, monkeypatch = loopback

    reg = op._groupem_box_register({"root": str(b)})
    assert reg["registered"] is True
    box_id = reg["box"]["box_id"]
    assert box_id != box_registry.LOCAL_BOX_ID
    assert reg["box"]["capability"].get("cpu_count")

    placed = op._groupem_box_place({"run": "r1"})
    assert placed["ok"] is True and placed["placement"]["box_id"] == box_id
    assert (b / "state" / "group-em" / "box" / "placements" / "r1.json").is_file()

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(b))
    assert box_heartbeat.stamp() is True
    posted = admission_post.post("START", "em-b", run="r1", require_holder=False)
    assert posted["delivered"] is True

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(a))
    roster = op._groupem_box_roster({})
    assert roster["boxes"] == [box_id]
    assert [(r["box_id"], r["session_id"], r["run"], r["live"]) for r in roster["rows"]] == [
        (box_id, "em-b", "r1", True)
    ]

    census = watch.box_census(str(a), {})
    assert census is not None and census["remote_boxes"] == roster["rows"]
    line = watch.box_line(census)
    assert f"box {box_id}: live, 1 open run(s): em-b r1" in line


def test_unstamped_loopback_box_is_unreachable_not_gone(loopback):
    a, b, _ = loopback
    box_id = op._groupem_box_register({"root": str(b)})["box"]["box_id"]

    census = watch.box_census(str(a), {})
    assert census is not None
    assert f"box {box_id}: unreachable" in watch.box_line(census)
