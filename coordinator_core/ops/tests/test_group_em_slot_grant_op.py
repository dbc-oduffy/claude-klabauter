"""
coordinator_core.ops.tests.test_group_em_slot_grant_op -- tests for "groupem.slot_grant" and
"groupem.slot_check" (registration, issue refused for a non-holder, check reads one path).
"""

from __future__ import annotations

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.group_em import nomination, slot_grant
from coordinator_core.group_em.suite_grant import IssueResult
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_slot_grant as op


def test_registration_quad():
    for key, cls in (
        ("groupem.slot_grant", OpClass.MUTATING),
        ("groupem.slot_check", OpClass.COMPUTE_ONLY),
    ):
        assert ipc._REGISTRY.get(key) is not None
        assert _registry_map.OP_MODULE_MAP[key] == "coordinator_core.ops.group_em_slot_grant"
        assert OP_KEY_SCOPE[key] == "none"
        assert OP_CLASSIFICATION[key] is cls


def test_issue_refused_for_non_holder(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(slot_grant.core, "resolve_session_id", lambda: "imposter")
    monkeypatch.setattr(
        nomination, "box_holder", lambda directory=None, now=None: {"session_id": "gem", "live": True}
    )
    out = op._groupem_slot_grant(
        {"grantee": "s1", "run": "r1", "declared_peak_mb": 4096, "note": "go"}
    )
    assert out["ok"] is False and out["record"] is None
    assert not list(tmp_path.rglob("*.json"))


def test_revoke_dispatches(monkeypatch):
    calls = []
    monkeypatch.setattr(
        slot_grant, "revoke_slot_grant", lambda g, r: calls.append((g, r)) or IssueResult(True, "absent", None)
    )
    out = op._groupem_slot_grant({"action": "revoke", "grantee": "s1", "run": "r1"})
    assert out == {"ok": True, "reason": "absent", "record": None}
    assert calls == [("s1", "r1")]


def test_check_reads_one_path(monkeypatch):
    seen = []
    monkeypatch.setattr(
        slot_grant, "check_slot_grant", lambda sid, run: seen.append((sid, run)) or (True, {"run": run})
    )
    out = op._groupem_slot_check({"session_id": "s1", "run": "r1"})
    assert out == {"granted": True, "record": {"run": "r1"}}
    assert seen == [("s1", "r1")]


def test_check_without_grant_is_ungranted(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(nomination, "box_holder", lambda directory=None, now=None: None)
    assert op._groupem_slot_check({"session_id": "s1", "run": "r1"}) == {"granted": False, "record": None}


def test_invalid_params_return_error():
    assert "error" in op._groupem_slot_grant({"grantee": "s1"})
    assert "error" in op._groupem_slot_grant({"action": "x", "grantee": "s1", "run": "r"})
    assert "error" in op._groupem_slot_check({"session_id": "s1"})
