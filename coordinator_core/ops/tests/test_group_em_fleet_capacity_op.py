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
from coordinator_core.group_em import (
    box_registry,
    box_transport,
    capacity_admit,
    capacity_probe,
    capacity_record,
    fleet_live,
    job_peaks,
)
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_fleet_capacity as op

_OPS = ("fleet_capacity", "capacity_admit", "capacity_place", "idle_capacity_watch")

_LIVE_SAMPLE = {
    "os": "Windows",
    "cores": 24,
    "ram_total_mb": 131072,
    "commit_limit_mb": 150000,
    "ram_free_mb": 50000,
    "commit_used_mb": 60000,
    "cpu_pct": 5.0,
    "sessions": 3,
    "jobs": [],
    "sampled_at": 1000.0,
}


def _far_unreachable(box: dict, op_name: str, params: dict, runner=None, *, timeout: float) -> dict:
    return {"ok": False, "state": "unknown", "reason": "ssh_unreachable"}


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
    monkeypatch.setattr(capacity_probe, "sample", lambda *a, **k: dict(_LIVE_SAMPLE))
    monkeypatch.setattr(box_transport, "invoke_remote", _far_unreachable)
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


def _far_ok(calls: list):
    def invoke(box: dict, op_name: str, params: dict, runner=None, *, timeout: float) -> dict:
        calls.append((box["box_id"], op_name, timeout))
        return {"ok": True, "reply": {"row": {**_LIVE_SAMPLE, "os": "Linux", "cores": 8, "sessions": 1}}}

    return invoke


def test_fleet_capacity_fans_out_one_capacity_local_invoke_per_far_box(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _peer(tmp_path, _sample(os_name="Linux"))
    calls: list = []
    monkeypatch.setattr(box_transport, "invoke_remote", _far_ok(calls))
    out = _call("fleet_capacity", {})
    assert [(c[0], c[1]) for c in calls] == [(peer, "groupem.capacity_local")]
    assert {b["box_id"] for b in out["boxes"]} == {out["local_box_id"], peer}
    assert [r["state"] for r in out["live"]] == ["ok", "ok"]
    assert {r["os"] for r in out["live"]} == {"Windows", "Linux"}
    assert all(r["alias"] for r in out["live"])
    assert out["table"].startswith("FLEET CAPACITY") and len(out["table"].splitlines()) == 3
    assert out["power_on"] == []
    assert fleet_live.read_last_known(fleet_live.last_known_path(home)).keys() == {out["local_box_id"], peer}


def test_fleet_capacity_unreachable_box_keeps_last_known_maxima_and_a_power_on_item(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _peer(tmp_path, _sample(os_name="Linux"))
    monkeypatch.setattr(box_transport, "invoke_remote", _far_ok([]))
    _call("fleet_capacity", {})
    monkeypatch.setattr(box_transport, "invoke_remote", _far_unreachable)
    out = _call("fleet_capacity", {})
    row = next(r for r in out["live"] if r["box"] == peer)
    assert row["state"] == "unreachable" and row["cores"] == 8 and row["last_seen"] == 1000.0
    assert row["cpu_pct"] is None and row["maxima_source"] == "last_known"
    assert len(out["power_on"]) == 1 and "UNREACHABLE" in out["power_on"][0]
    assert any("UNREACHABLE" in line and "unknown (ssh_unreachable)" in line for line in out["table"].splitlines())
    assert len(out["live"]) == 2


def test_fleet_capacity_a_hung_box_times_out_without_blocking_the_rest(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _peer(tmp_path, _sample(os_name="Linux"))

    def hang(box: dict, op_name: str, params: dict, runner=None, *, timeout: float) -> dict:
        time.sleep(3)
        return {"ok": True, "reply": {"row": dict(_LIVE_SAMPLE)}}

    monkeypatch.setattr(box_transport, "invoke_remote", hang)
    t0 = time.monotonic()
    out = _call("fleet_capacity", {"timeout": 0.3})
    assert time.monotonic() - t0 < 2.0
    states = {r["box"]: r["state"] for r in out["live"]}
    assert states[peer] == "timed_out" and states[out["local_box_id"]] == "ok"


def test_fleet_capacity_local_only_makes_no_remote_call(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _peer(tmp_path, _sample(os_name="Linux"))
    calls: list = []
    monkeypatch.setattr(box_transport, "invoke_remote", _far_ok(calls))
    out = _call("fleet_capacity", {"local_only": True})
    assert calls == []
    assert [r["box"] for r in out["live"]] == [out["local_box_id"]] and out["live"][0]["state"] == "ok"
    assert len(out["boxes"]) == 2


def test_fleet_capacity_per_box_timeout_default_and_clamp_sit_under_the_ipc_backstop(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _peer(tmp_path, _sample(os_name="Linux"))
    calls: list = []
    monkeypatch.setattr(box_transport, "invoke_remote", _far_ok(calls))
    _call("fleet_capacity", {})
    _call("fleet_capacity", {"timeout": 100000})
    default, clamped = calls[0][2], calls[1][2]
    backstop = ipc._timeout_for("groupem.fleet_capacity")
    assert default == op._PER_BOX_TIMEOUT_S == 8.0
    assert default < backstop and clamped < backstop and clamped * 1.1 < backstop
    assert _call("fleet_capacity", {"timeout": 1})["live"]


@pytest.mark.parametrize("params", [{"local_only": "yes"}, {"timeout": 0}, {"timeout": -1}, {"timeout": "5"}, {"timeout": True}])
def test_fleet_capacity_bad_params_return_error(home: Path, params: dict) -> None:
    assert "error" in op._groupem_fleet_capacity(params)


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
