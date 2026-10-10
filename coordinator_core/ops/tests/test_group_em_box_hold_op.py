"""
coordinator_core.ops.tests.test_group_em_box_hold_op -- tests for "groupem.box_hold"
(registration, set/clear round-trip, invalid params).
"""

from __future__ import annotations

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.group_em import box_hold
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_box_hold as op


def test_registration_quad():
    assert ipc._REGISTRY.get("groupem.box_hold") is not None
    assert _registry_map.OP_MODULE_MAP["groupem.box_hold"] == "coordinator_core.ops.group_em_box_hold"
    assert OP_KEY_SCOPE["groupem.box_hold"] == "none"
    assert OP_CLASSIFICATION["groupem.box_hold"] is OpClass.MUTATING


def test_set_then_clear_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    out = op._groupem_box_hold({"action": "set", "session_id": "s1", "reason": "swap", "ttl_s": 60})
    assert out["hold"]["set_by_session"] == "s1"
    live = box_hold.read_hold()
    assert live is not None and live.reason == "swap"
    assert op._groupem_box_hold({"action": "clear", "session_id": "other"})["cleared"] is False
    out = op._groupem_box_hold({"action": "clear", "session_id": "s1"})
    assert out == {"action": "clear", "cleared": True, "operator": False}
    assert box_hold.read_hold() is None


def test_operator_clear_omits_session(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    op._groupem_box_hold({"action": "set", "session_id": "s1", "reason": "r"})
    out = op._groupem_box_hold({"action": "clear"})
    assert out == {"action": "clear", "cleared": True, "operator": True}


def test_set_ttl_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    h = op._groupem_box_hold({"action": "set", "session_id": "s1", "ttl_s": 10**9})["hold"]
    assert h["expires_at"] - h["set_at"] == box_hold.MAX_HOLD_TTL_S


def test_invalid_params_return_error(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert "error" in op._groupem_box_hold({"action": "bogus"})
    assert "error" in op._groupem_box_hold({})
    assert "error" in op._groupem_box_hold({"action": "set"})
    assert "error" in op._groupem_box_hold({"action": "set", "session_id": "s", "ttl_s": "x"})
    assert "error" in op._groupem_box_hold({"action": "clear", "session_id": 5})
    assert box_hold.read_hold() is None
