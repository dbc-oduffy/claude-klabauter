"""Fixture-only tests for telemetry.traffic_coverage."""

import json

from coordinator_core.telemetry import traffic_coverage as tc


def _manifest(resident, ops):
    return {
        "legs": {
            "engine_served": {"status": "measured", "total": sum(o["count"] for o in ops.values()), "ops": ops},
            "resident_bash": resident,
        }
    }


PENDING = {"status": "pending", "blocked_on": "B-β", "total": None, "entry_points": None}


def _patch_served(monkeypatch, keys):
    monkeypatch.setattr(tc, "served_ops", lambda: frozenset(keys))


def _row(n):
    return {"count": n, "by_origin": {}, "by_caller": {}, "errors": 0}


def test_served_ops_is_registry_keys():
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert tc.served_ops() == frozenset(OP_MODULE_MAP)


def test_pending_leg_refuses_ratio(monkeypatch):
    _patch_served(monkeypatch, {"a"})
    out = tc.coverage(_manifest(PENDING, {"a": _row(5)}))
    assert out["kr4_ratio"] is None
    assert out["kr4_reason"] == "resident_bash leg pending (B-β)"
    assert out["served_count"] == 5


def test_measured_leg_ratio(monkeypatch):
    _patch_served(monkeypatch, {"a"})
    resident = {"status": "measured", "total": 10, "entry_points": {"x.sh": {"count": 10}}}
    out = tc.coverage(_manifest(resident, {"a": _row(6), "b": _row(4)}))
    assert out["kr4_ratio"] == 6 / 20
    assert out["kr4_reason"] is None


def test_unserved_split(monkeypatch):
    _patch_served(monkeypatch, {"a"})
    out = tc.coverage(_manifest(PENDING, {"a": _row(1), "z": _row(2), "y": _row(9)}))
    assert out["unserved"] == [{"op": "y", "count": 9}, {"op": "z", "count": 2}]
    assert out["served_count"] == 1


def test_reconcile_names_synthetic_extra_op(monkeypatch, tmp_path):
    from coordinator_core.authz.classification import OP_CLASSIFICATION
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps([{"op_key": "inv.only"}, {"op_key": "shared"}]))
    monkeypatch.setattr(tc, "served_ops", lambda: frozenset({"shared", "synthetic.extra"}))
    out = tc.reconcile(inv)
    assert set(out) == {"op-inventory.json", "OP_CLASSIFICATION", "_OP_KEY_SCOPE"}
    assert out["op-inventory.json"]["served_not_in_table"] == ["synthetic.extra"]
    assert out["op-inventory.json"]["table_not_served"] == ["inv.only"]
    assert "synthetic.extra" in out["OP_CLASSIFICATION"]["served_not_in_table"]
    assert out["_OP_KEY_SCOPE"]["table_size"] == len(_OP_KEY_SCOPE)
    assert out["OP_CLASSIFICATION"]["table_size"] == len(OP_CLASSIFICATION)


def test_reconcile_real_inventory_runs():
    out = tc.reconcile()
    assert out["op-inventory.json"]["table_size"] > 0
