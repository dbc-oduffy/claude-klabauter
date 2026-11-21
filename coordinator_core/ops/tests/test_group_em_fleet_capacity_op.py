"""
coordinator_core.ops.tests.test_group_em_fleet_capacity_op -- the four capacity ops through the
dispatch entry, over a tmp settings home holding stamped samples. Every write is under tmp_path.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.group_em import box_registry, capacity_admit, capacity_record, job_peaks
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_fleet_capacity as op

_OPS = ("fleet_capacity", "capacity_admit", "capacity_place", "idle_capacity_watch")


def _call(name: str, params: dict) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": f"groupem.{name}", "params": params}
    out = asyncio.run(ipc.dispatch_message(msg))
    assert "error" not in out or out.get("result") is not None, out
    return out["result"]


def _sample(total=32768, used=6144, util=1.0, os_name="Windows", ram=50000) -> dict:
    return {
        "os": os_name,
        "arch": "x86_64",
        "cores_physical": 16,
        "cores_logical": 24,
        "load_pct": 5.0,
        "ram_total_mb": 131072,
        "ram_avail_mb": ram,
        "gpus": [{"index": 0, "name": "g", "vram_total_mib": total, "vram_used_mib": used, "util_pct": util}],
        "jobs": [],
    }


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(h))
    monkeypatch.setattr(op, "_refresh_local", lambda: False)
    box_registry.ensure_identity(h)
    assert capacity_record.stamp(_sample(), h)
    job_peaks.record("fits", {"vram_mib": 20000, "ram_mb": 4000, "cores": 2}, h)
    job_peaks.record("big", {"vram_mib": 30000}, h)
    job_peaks.record("half", {"vram_mib": 12000}, h)
    return h


def _peer(tmp_path: Path, sample: dict) -> str:
    root = tmp_path / "peer"
    root.mkdir()
    peer_id = box_registry.ensure_identity(root)
    box_registry.register(root, {}, box_id=peer_id)
    assert capacity_record.stamp(sample, root)
    return peer_id


@pytest.mark.parametrize("name", _OPS)
def test_registered_in_registry_and_map(name: str) -> None:
    key = f"groupem.{name}"
    assert ipc._REGISTRY.get(key) is not None
    assert _registry_map.OP_MODULE_MAP[key] == "coordinator_core.ops.group_em_fleet_capacity"


def test_fleet_capacity_reports_local_measured_fields(home: Path) -> None:
    out = _call("fleet_capacity", {})
    row = next(b for b in out["boxes"] if b["box_id"] == out["local_box_id"])
    assert row["stale"] is False and row["os"] == "Windows"
    assert row["gpus"][0]["vram_total_mib"] == 32768 and row["gpus"][0]["vram_used_mib"] == 6144
    assert row["ram_total_mb"] == 131072 and row["ram_avail_mb"] == 50000 and row["cores"] == 24
    assert "age_s" in row and "jobs" in row


def test_fleet_capacity_stale_peer_has_no_made_up_zero(home: Path, tmp_path: Path) -> None:
    root = tmp_path / "dark"
    root.mkdir()
    dark = box_registry.ensure_identity(root)
    box_registry.register(root, {}, box_id=dark)
    row = next(b for b in _call("fleet_capacity", {})["boxes"] if b["box_id"] == dark)
    assert row["stale"] is True and row["age_s"] is None
    assert "ram_avail_mb" not in row and "gpus" not in row


def test_fleet_capacity_stale_local_is_restamped(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capacity_record._path(home).unlink()
    monkeypatch.setattr(op, "_refresh_local", lambda: capacity_record.stamp(_sample(), home))
    row = next(b for b in _call("fleet_capacity", {})["boxes"])
    assert row["stale"] is False and row["refreshed"] is True


def test_local_root_registered_under_another_id_answers_to_local_id(home: Path) -> None:
    box_registry.register(home, {}, box_id="Mac-mini")
    out = _call("fleet_capacity", {})
    assert [b["box_id"] for b in out["boxes"]] == [out["local_box_id"]]
    assert out["boxes"][0]["stale"] is False
    reply = _call("capacity_admit", {"job_type": "fits", "box_id": out["local_box_id"]})
    assert reply["grant"] is True and reply["box_id"] == out["local_box_id"]


def test_admit_on_local_root_under_other_registry_id_grants_then_refuses_naming_vram(home: Path) -> None:
    box_registry.register(home, {}, box_id="Mac-mini")
    job_peaks.record("ten", {"vram_mib": 10000}, home)
    local = box_registry.local_box_id(home)
    for run in ("a", "b"):
        out = _call("capacity_admit", {"job_type": "ten", "run": run})
        assert out["grant"] is True and out["box_id"] == local and "error" not in out
    third = _call("capacity_admit", {"job_type": "ten", "run": "c"})
    assert "error" not in third and third["grant"] is False
    assert third["short"][0]["resource"] == "vram" and third["short"][0]["need"] == 10000
    assert third["short"][0]["free"] == 32768 - 6144 - 20000
    assert third["capable_elsewhere"] == []
    by_registry_id = _call("capacity_admit", {"job_type": "ten", "run": "d", "box_id": "Mac-mini"})
    assert "error" not in by_registry_id and by_registry_id["grant"] is False
    assert by_registry_id["short"][0]["resource"] == "vram" and by_registry_id["box_id"] == local


def test_admit_unknown_box_id_is_still_an_error(home: Path) -> None:
    assert "error" in _call("capacity_admit", {"job_type": "fits", "run": "x", "box_id": "nowhere"})


def test_admit_grants_when_peak_fits_32g_box_with_6g_used(home: Path) -> None:
    out = _call("capacity_admit", {"job_type": "fits", "run": "r1"})
    assert out["grant"] is True and out["short"] == [] and out["peak_basis"] == "measured"
    assert not any("slot" in k for k in out)


def test_admit_refuses_naming_measured_vram_shortfall(home: Path) -> None:
    out = _call("capacity_admit", {"job_type": "big", "run": "r1", "priority": "priority", "requester_sid": "s9"})
    assert out["grant"] is False
    assert out["short"][0]["resource"] == "vram" and out["short"][0]["need"] == 30000
    assert out["short"][0]["free"] == 32768 - 6144
    assert out["capable_elsewhere"] == []
    wait = capacity_admit.read_waits(home)[0]
    assert wait["run"] == "r1" and wait["requester_sid"] == "s9"


def test_admit_two_that_fit_then_third_refuses_on_vram(home: Path) -> None:
    assert _call("capacity_admit", {"job_type": "half", "run": "a"})["grant"] is True
    assert _call("capacity_admit", {"job_type": "half", "run": "b"})["grant"] is True
    third = _call("capacity_admit", {"job_type": "half", "run": "c"})
    assert third["grant"] is False and third["short"][0]["resource"] == "vram"


def test_admit_unmeasured_names_measurement_and_declared_is_labelled(home: Path) -> None:
    out = _call("capacity_admit", {"job_type": "never-run", "run": "u"})
    assert out["grant"] is False and out["short"] == [{"resource": "measurement", "reason": "no_measured_peak"}]
    dec = _call("capacity_admit", {"job_type": "never-run", "run": "u2", "declared": {"vram_mib": 1000}})
    assert dec["grant"] is True and dec["peak_basis"] == "declared"


def test_admit_refusal_lists_capable_elsewhere(home: Path, tmp_path: Path) -> None:
    capacity_record.stamp(_sample(total=8192, used=6144), home)
    peer = _peer(tmp_path, _sample(os_name="Linux"))
    local = box_registry.local_box_id(home)
    out = _call("capacity_admit", {"job_type": "fits", "run": "r", "box_id": local})
    assert out["grant"] is False and out["box_id"] == local
    assert [c["box_id"] for c in out["capable_elsewhere"]] == [peer]
    free = _call("capacity_admit", {"job_type": "fits", "run": "r2"})
    assert free["grant"] is True and free["box_id"] == peer


def test_place_lists_capable_boxes_and_filters_by_os(home: Path, tmp_path: Path) -> None:
    peer = _peer(tmp_path, _sample(os_name="Linux"))
    both = _call("capacity_place", {"job_type": "fits"})["boxes"]
    assert {b["box_id"] for b in both} == {box_registry.local_box_id(home), peer}
    assert {b["os"] for b in both} == {"Windows", "Linux"}
    linux = _call("capacity_place", {"job_type": "fits", "os": "Linux"})["boxes"]
    assert [b["box_id"] for b in linux] == [peer]
    assert _call("capacity_place", {"job_type": "never-run"})["reason"] == "no_measured_peak"


def test_idle_watch_alerts_on_priority_wait_with_idle_box(home: Path) -> None:
    wait = {"run": "w1", "job_type": "fits", "requester_sid": "req", "priority": "priority", "since": time.time() - 1000}
    capacity_admit.waits_path(home).write_text(json.dumps(wait) + "\n", encoding="utf-8")
    out = _call("idle_capacity_watch", {})
    assert len(out["alerts"]) == 1
    alert = out["alerts"][0]
    assert alert["run"] == "w1" and alert["requester_sid"] == "req"
    assert alert["idle_box_id"] == box_registry.local_box_id(home)


def test_idle_watch_skips_the_holder_lookup_when_nothing_waits(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.group_em import nomination

    def boom() -> None:
        raise AssertionError("box_holder snapshots the session registry; no wait means no lookup")

    monkeypatch.setattr(nomination, "box_holder", boom)
    out = _call("idle_capacity_watch", {})
    assert out["alerts"] == [] and out["waits"] == 0


def test_idle_watch_quiet_without_priority_wait_or_when_box_busy(home: Path) -> None:
    assert _call("idle_capacity_watch", {})["alerts"] == []
    wait = {"run": "w", "job_type": "fits", "priority": "priority", "since": time.time() - 1000}
    capacity_admit.waits_path(home).write_text(json.dumps(wait) + "\n", encoding="utf-8")
    capacity_record.stamp(_sample(util=95.0), home)
    assert _call("idle_capacity_watch", {})["alerts"] == []


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("capacity_admit", {}),
        ("capacity_admit", {"job_type": "x", "run": 3}),
        ("capacity_admit", {"job_type": "x", "os": 5}),
        ("capacity_admit", {"job_type": "x", "declared": {"vram_mib": "lots"}}),
        ("capacity_place", {"job_type": "x", "requirement": "no"}),
        ("capacity_place", {}),
    ],
)
def test_bad_params_return_error(home: Path, name: str, params: dict) -> None:
    assert "error" in getattr(op, f"_groupem_{name}")(params)
    assert "error" in getattr(op, f"_groupem_{name}")(None)


def test_admit_refusal_wait_row_carries_target_box_id(home: Path) -> None:
    capacity_record.stamp(_sample(total=8192, used=6144), home)
    local = box_registry.local_box_id(home)
    out = _call("capacity_admit", {"job_type": "fits", "run": "w", "box_id": local})
    assert out["grant"] is False
    rows = [json.loads(x) for x in capacity_admit.waits_path(home).read_text(encoding="utf-8").splitlines() if x]
    assert [r["box_id"] for r in rows if r["run"] == "w"] == [local]
