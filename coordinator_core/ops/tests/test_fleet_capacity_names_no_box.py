"""
coordinator_core.ops.tests.test_fleet_capacity_names_no_box -- groupem.fleet_capacity carries no box
name: it discovers boxes from box_registry, so doctrine can name the op alone. Registry under tmp_path.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.group_em import box_registry, box_transport, capacity_probe, capacity_record, fleet_live
from coordinator_core.ops import group_em_fleet_capacity as op

_REPO = Path(__file__).resolve().parents[3]
_KNOWN_BOX_NAMES = re.compile(r"machine-a|gipsy|mac-?mini|machine-b|machine-c|box-[0-9a-f]{8}", re.IGNORECASE)
_FIXTURE_BOXES = ("zz-alpha", "zz-beta", "zz-gamma")

_LIVE = {
    "os": "Linux",
    "cores": 8,
    "ram_total_mb": 65536,
    "commit_limit_mb": 70000,
    "ram_free_mb": 30000,
    "commit_used_mb": 20000,
    "cpu_pct": 4.0,
    "sessions": 1,
    "jobs": [],
    "sampled_at": 1000.0,
}


def _sample() -> dict:
    return {
        "os": "Linux",
        "arch": "x86_64",
        "cores_physical": 4,
        "cores_logical": 8,
        "load_pct": 4.0,
        "ram_total_mb": 65536,
        "ram_avail_mb": 30000,
        "gpus": [{"index": 0, "name": "g", "vram_total_mib": 8192, "vram_used_mib": 1024, "util_pct": 1.0}],
        "jobs": [],
    }


def _call(params: dict) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "groupem.fleet_capacity", "params": params}
    return asyncio.run(ipc.dispatch_message(msg))["result"]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(h))
    monkeypatch.setattr(op, "_refresh_local", lambda: False)
    monkeypatch.setattr(capacity_probe, "sample", lambda *a, **k: dict(_LIVE))
    calls: list = []

    def invoke(box: dict, op_name: str, params: dict, runner=None, *, timeout: float) -> dict:
        calls.append(box["box_id"])
        return {"ok": True, "reply": {"row": dict(_LIVE)}}

    monkeypatch.setattr(box_transport, "invoke_remote", invoke)
    box_registry.ensure_identity(h)
    assert capacity_record.stamp(_sample(), h)
    return h


def _register_fixture_boxes(tmp_path: Path) -> None:
    for name in _FIXTURE_BOXES:
        root = tmp_path / name
        root.mkdir()
        box_registry.register(root, {}, box_id=name)
        assert capacity_record.stamp(_sample(), root)


def test_op_source_and_catalog_entry_name_no_box() -> None:
    sources = {
        "op module": inspect.getsource(op),
        "fleet_live": inspect.getsource(fleet_live),
        "box_registry": inspect.getsource(box_registry),
    }
    catalog = json.loads((_REPO / "coordinator_core/contract/op-catalog.json").read_text(encoding="utf-8"))
    entries = [e for e in _iter_dicts(catalog) if e.get("name") == "groupem.fleet_capacity"]
    assert entries, "groupem.fleet_capacity missing from the op catalog"
    sources["catalog entry"] = json.dumps(entries)
    hits = {k: sorted(set(_KNOWN_BOX_NAMES.findall(v))) for k, v in sources.items() if _KNOWN_BOX_NAMES.search(v)}
    assert hits == {}


def _iter_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_dicts(v)


def test_every_registered_box_is_reported_once_whatever_it_is_named(home: Path, tmp_path: Path) -> None:
    _register_fixture_boxes(tmp_path)
    out = _call({})
    expected = {out["local_box_id"], *_FIXTURE_BOXES}
    ids = [b["box_id"] for b in out["boxes"]]
    assert set(ids) == expected and len(ids) == len(set(ids))
    live = [r["box"] for r in out["live"]]
    assert set(live) == expected and len(live) == len(set(live))
    assert {r["state"] for r in out["live"]} == {"ok"}


def test_a_box_the_registry_lacks_is_absent_and_the_registered_one_is_present(home: Path, tmp_path: Path) -> None:
    before = {b["box_id"] for b in _call({})["boxes"]}
    assert before == {box_registry.local_box_id(home)}
    _register_fixture_boxes(tmp_path)
    after = {b["box_id"] for b in _call({})["boxes"]}
    assert after == before | set(_FIXTURE_BOXES)


def test_local_root_registered_under_another_id_is_not_listed_twice(home: Path, tmp_path: Path) -> None:
    box_registry.register(home, {}, box_id="zz-local-alias")
    _register_fixture_boxes(tmp_path)
    out = _call({})
    local = out["local_box_id"]
    ids = [b["box_id"] for b in out["boxes"]]
    assert ids.count(local) == 1 and "zz-local-alias" not in ids
    live = [r["box"] for r in out["live"]]
    assert live.count(local) == 1 and "zz-local-alias" not in live
