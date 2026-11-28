"""
coordinator_core.ops.tests.test_group_em_fleet_book_op -- `groupem.fleet_book` through the dispatch
entry, over a tmp settings home holding a stamped local sample, registered far boxes and a persisted
fleet live snapshot. Every write is under tmp_path; no ssh runs.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.group_em import box_registry, box_transport, capacity_admit, capacity_record, fleet_live, job_peaks
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_fleet_book as op


def _call(params: dict) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "groupem.fleet_book", "params": params}
    out = asyncio.run(ipc.dispatch_message(msg))
    assert "error" not in out or out.get("result") is not None, out
    return out["result"]


def _local_sample(ram: int = 20000, load: float = 80.0) -> dict:
    return {
        "os": "Windows", "arch": "x86_64", "cores_physical": 8, "cores_logical": 16, "load_pct": load,
        "ram_total_mb": 65536, "ram_avail_mb": ram, "jobs": [],
        "gpus": [{"index": 0, "name": "g", "vram_total_mib": 24000, "vram_used_mib": 20000, "util_pct": 90.0}],
    }


def _far(home: Path, box_id: str, host: str) -> None:
    box_registry.register(
        "/fleet/" + box_id, {}, box_id=box_id, transport={"kind": "ssh", "host": f"u@{host}.example"}
    )


def _row(box: str, alias: str, at: float, state: str = "ok", **use) -> dict:
    row = fleet_live.empty_row(box, state)
    row.update(alias=alias, sampled_at=at, **use)
    return row


def _snapshot(home: Path, rows: list, at: float) -> None:
    fleet_live.write_live_snapshot(rows, at, path=fleet_live.live_snapshot_path(home))


def _ledger(home: Path) -> list:
    path = capacity_admit.grants_path(home)
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x] if path.exists() else []


def _by_id(out: dict) -> dict:
    return {b["box_id"]: b for b in out["boxes"]}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(h))

    def _no_network(*a, **k):
        raise AssertionError("fleet_book must never reach a far box")

    monkeypatch.setattr(box_transport, "invoke_remote", _no_network)
    box_registry.ensure_identity(h)
    assert capacity_record.stamp(_local_sample(), h)
    return h


@pytest.fixture()
def fleet(home: Path) -> dict:
    """Local box busy; ``hetero`` (P/E cores, mostly free P) and ``mini`` (homogeneous, most free cores) far."""
    _far(home, "hetero", "hetero")
    _far(home, "mini", "mini")
    now = time.time()
    rows = [
        _row("hetero", "hetero", now, cores=24, cpu_pct=10.0, ram_free_mb=90000,
             core_classes={"p": list(range(8)), "e": list(range(8, 24))},
             cores_by_class={"p": 8, "e": 16}, free_cores_by_class={"p": 7, "e": 15}),
        _row("mini", "mini", now, cores=64, cpu_pct=20.0, ram_free_mb=40000),
    ]
    _snapshot(home, rows, now)
    return {"now": now}


def test_registered_and_classified() -> None:
    assert _registry_map.OP_MODULE_MAP["groupem.fleet_book"] == "coordinator_core.ops.group_em_fleet_book"
    assert OP_CLASSIFICATION["groupem.fleet_book"] is OpClass.MUTATING
    assert ipc.get_op_handler("groupem.fleet_book") is op._groupem_fleet_book


def test_job_type_is_required(home: Path) -> None:
    assert "job_type" in _call({})["error"]
    assert "declared" in _call({"job_type": "x", "declared": "no"})["error"]
    assert "core_class" in _call({"job_type": "x", "declared": {"core_class": ""}})["error"]


def test_booking_needs_no_priority_or_peak_and_lists_every_box(home: Path, fleet: dict) -> None:
    out = _call({"job_type": "x"})
    local = box_registry.local_box_id(home)
    assert out["booked"] is True and out["recorded"] is True
    assert out["basis"] == "unmeasured"
    assert out["box_id"] == "mini"
    ids = [b["box_id"] for b in out["boxes"]]
    assert sorted(ids) == sorted([local, "hetero", "mini"]) and len(set(ids)) == 3
    rows = [r for r in _ledger(home) if r["run"] == out["run"]]
    assert len(rows) == 1 and rows[0]["box_id"] == "mini" and rows[0]["basis"] == "unmeasured"
    assert rows[0]["job_type"] == "x" and rows[0]["ram_mb"] is None


def test_heterogeneous_box_reports_classes_and_homogeneous_a_count(home: Path, fleet: dict) -> None:
    boxes = _by_id(_call({"job_type": "x"}))
    assert boxes["hetero"]["free_cores_by_class"] == {"p": 7, "e": 15}
    assert boxes["hetero"]["free_cores"] is None
    assert boxes["mini"]["free_cores_by_class"] is None
    assert boxes["mini"]["free_cores"] == pytest.approx(64 * 0.8, abs=0.1)
    local = boxes[box_registry.local_box_id(home)]
    assert local["state"] == "ok" and local["ram_free_mb"] == 20000 and local["vram_free_mib"] == 4000
    assert local["gpu_util_pct"] == 90.0 and local["idle"] is False


def test_default_pick_is_the_box_with_most_free_cores(home: Path, fleet: dict) -> None:
    out = _call({"job_type": "x"})
    assert out["box_id"] == "mini" and not out.get("class_fallback")


def test_core_class_picks_by_that_classes_free_cores(home: Path, fleet: dict) -> None:
    out = _call({"job_type": "x", "declared": {"core_class": "p"}})
    assert out["box_id"] == "hetero"
    assert [r for r in _ledger(home) if r["run"] == out["run"]][0]["core_class"] == "p"


def test_core_class_no_box_carries_falls_back_to_a_usable_box(home: Path, fleet: dict) -> None:
    out = _call({"job_type": "x", "declared": {"core_class": "q"}})
    assert out["class_fallback"] is True and out["box_id"] in {"hetero", "mini"}


def test_stale_and_unreachable_boxes_are_listed_never_picked_and_never_idle(home: Path) -> None:
    _far(home, "old", "old")
    _far(home, "dark", "dark")
    now = time.time()
    _snapshot(home, [
        _row("old", "old", now - 3600, cores=128, cpu_pct=0.0, ram_free_mb=900000),
        _row("dark", "dark", now, state="unreachable", cores=128, cpu_pct=0.0, ram_free_mb=900000),
    ], now)
    out = _call({"job_type": "x"})
    boxes = _by_id(out)
    assert boxes["old"]["state"] == "stale" and boxes["dark"]["state"] == "unreachable"
    for name in ("old", "dark"):
        assert boxes[name]["idle"] is False
        assert boxes[name]["ram_free_mb"] is None and boxes[name]["free_cores"] is None
    assert out["box_id"] == box_registry.local_box_id(home)


def test_never_sampled_registered_box_is_stale_not_free(home: Path) -> None:
    _far(home, "ghost", "ghost")
    boxes = _by_id(_call({"job_type": "x"}))
    assert boxes["ghost"]["state"] == "stale" and boxes["ghost"]["age_s"] is None
    assert boxes["ghost"]["idle"] is False


def test_no_fresh_box_anywhere_books_on_this_box_with_a_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = tmp_path / "bare"
    h.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(h))
    box_registry.ensure_identity(h)
    out = _call({"job_type": "x"})
    assert out["reason"] == "no_fresh_box" and out["box_id"] == box_registry.local_box_id(h)
    assert out["recorded"] is True and _ledger(h)[0]["box_id"] == out["box_id"]


def test_explicit_box_by_alias_and_unknown_box(home: Path, fleet: dict) -> None:
    out = _call({"job_type": "x", "box_id": "MINI"})
    assert out["box_id"] == "mini" and out["alias"] == "mini"
    assert "not registered" in _call({"job_type": "x", "box_id": "nowhere"})["error"]


def test_far_booking_reduces_that_box_headroom_only(home: Path, fleet: dict) -> None:
    before = _by_id(_call({"job_type": "probe", "box_id": "mini", "run": "r0"}))
    out = _call({"job_type": "x", "box_id": "mini", "run": "r1", "declared": {"ram_mb": 10000}})
    after = _by_id(out)
    local = box_registry.local_box_id(home)
    assert out["basis"] == "declared"
    assert after["mini"]["ram_free_mb"] == before["mini"]["ram_free_mb"] - 10000
    assert after[local]["ram_free_mb"] == before[local]["ram_free_mb"]
    assert after["hetero"]["ram_free_mb"] == before["hetero"]["ram_free_mb"]


def test_a_recorded_peak_labels_the_booking_measured(home: Path, fleet: dict) -> None:
    job_peaks.record("known", {"ram_mb": 1000, "cores": 2}, home)
    out = _call({"job_type": "known"})
    assert out["basis"] == "measured"


def test_a_repeat_run_is_one_ledger_row(home: Path, fleet: dict) -> None:
    _call({"job_type": "x", "run": "same"})
    _call({"job_type": "x", "run": "same", "box_id": "mini"})
    rows = [r for r in _ledger(home) if r["run"] == "same"]
    assert len(rows) == 1 and rows[0]["box_id"] == "mini"


def test_it_never_reaches_a_far_box(home: Path, fleet: dict) -> None:
    assert _call({"job_type": "x"})["booked"] is True  # invoke_remote raises in the fixture if called
