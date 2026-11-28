"""
coordinator_core.ops.tests.test_group_em_capacity_local_op -- `groupem.capacity_local` is a registered,
COMPUTE_ONLY op that a far box's coordinator-invoke may run, and it never raises.
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core import ipc, op_scopes
from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.group_em import box_transport, capacity_probe, fleet_live
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_capacity_local as op
from coordinator_core.remote_engine import policy

_OP = "groupem.capacity_local"


def _call_op(method: str, params: dict | None = None) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return asyncio.run(ipc.dispatch_message(msg))["result"]


def _call(params: dict) -> dict:
    return _call_op(_OP, params)


def test_registered_in_registry_map_and_scopes() -> None:
    assert ipc._REGISTRY.get(_OP) is not None
    assert _registry_map.OP_MODULE_MAP[_OP] == "coordinator_core.ops.group_em_capacity_local"
    assert op_scopes._OP_KEY_SCOPE[_OP] == "none"


def test_compute_only_and_remote_safe() -> None:
    assert classify(_OP) is OpClass.COMPUTE_ONLY
    assert policy.remote_safe(_OP) is True
    assert box_transport._op_allowed(_OP) is True


def test_a_mutating_sibling_stays_refused_on_a_far_box() -> None:
    assert box_transport._op_allowed("groupem.capacity_admit") is False
    assert box_transport._op_allowed("groupem.job_progress") is False
    assert "groupem.job_progress" not in policy.PHASE2_MUTATING_ALLOWLIST


def test_returns_the_probe_row_through_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    canned = fleet_live.empty_row("box-x", fleet_live.STATE_OK)
    monkeypatch.setattr(capacity_probe, "sample", lambda *a, **k: canned)
    out = _call({})
    assert out == {"row": canned}
    assert set(out["row"]) == set(fleet_live.FIELDS)


def test_a_sampler_failure_is_an_error_reply_not_a_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("no sampler")

    monkeypatch.setattr(capacity_probe, "sample", boom)
    assert _call({}) == {"error": "RuntimeError: no sampler"}


def test_non_dict_params_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capacity_probe, "sample", lambda *a, **k: {"box": "b"})
    assert op._groupem_capacity_local(None) == {"row": {"box": "b"}}


_REPORT = "groupem.file_cache_report"
_TRIM = "groupem.file_cache_trim"


def test_file_cache_ops_registered_and_classified() -> None:
    for name in (_REPORT, _TRIM):
        assert ipc._REGISTRY.get(name) is not None
        assert _registry_map.OP_MODULE_MAP[name] == "coordinator_core.ops.group_em_capacity_local"
        assert op_scopes._OP_KEY_SCOPE[name] == "none"
    assert classify(_REPORT) is OpClass.COMPUTE_ONLY
    assert classify(_TRIM) is OpClass.MUTATING
    assert box_transport._op_allowed(_REPORT) is True
    assert box_transport._op_allowed(_TRIM) is True
    assert policy.PHASE2_MUTATING_ALLOWLIST >= {_TRIM}
    assert _REPORT not in policy.PHASE2_MUTATING_ALLOWLIST


def test_file_cache_report_serves_local_report_through_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    fct = op._file_cache_trim()
    monkeypatch.setattr(fct, "local_report", lambda *a, **k: {"status": "not_applicable", "os": "linux"})
    assert _call_op(_REPORT) == {"status": "not_applicable", "os": "linux"}


def test_file_cache_trim_serves_local_trim_through_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    fct = op._file_cache_trim()
    monkeypatch.setattr(fct, "local_trim", lambda *a, **k: {"status": "ok", "os": "win32", "exit": 0})
    assert _call_op(_TRIM) == {"status": "ok", "os": "win32", "exit": 0}


def test_file_cache_ops_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    fct = op._file_cache_trim()

    def boom(*a, **k):
        raise RuntimeError("no host")

    monkeypatch.setattr(fct, "local_report", boom)
    monkeypatch.setattr(fct, "local_trim", boom)
    assert _call_op(_REPORT) == {"status": "error", "detail": "RuntimeError: no host"}
    assert _call_op(_TRIM) == {"status": "error", "detail": "RuntimeError: no host"}


def test_real_report_is_a_status_dict_and_trims_nothing() -> None:
    out = _call_op(_REPORT)
    assert out["status"] in {"ok", "not_applicable", "error"}


def test_real_sample_has_the_fields() -> None:
    out = _call({})
    assert "error" not in out and set(out["row"]) == set(fleet_live.FIELDS)


def test_a_declared_split_rides_the_reply_and_an_undeclared_box_adds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capacity_probe, "sample", lambda *a, **k: {"box": "b", "cores": 3})
    monkeypatch.setattr(fleet_live, "read_core_classes", lambda home=None: None)
    assert _call({}) == {"row": {"box": "b", "cores": 3}}
    monkeypatch.setattr(fleet_live, "read_core_classes", lambda home=None: {"p": [0], "e": [1, 2]})
    monkeypatch.setattr(fleet_live, "read_core_busy_pct", lambda: [0.0, 90.0, 0.0])
    row = _call({})["row"]
    assert row["cores_by_class"] == {"p": 1, "e": 2} and row["free_cores_by_class"] == {"p": 1, "e": 1}
