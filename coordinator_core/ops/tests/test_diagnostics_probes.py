"""
coordinator_core.ops.tests.test_diagnostics_probes

In-process tests for the three write-free `diagnostics.*` transport-failure probes.

Scope, deliberately narrow: registration presence (each op reaches the live `_REGISTRY`
through the ordinary `@register_op` seam) and the in-process return/raise shapes. This
module is SPAWN-FREE — it starts no child process, import-time or otherwise. The
end-to-end assertions (rc=1 with the error on stdout, rc=2/`StructuralPinError` through
a real `coordinator_core.invoke` child) live in `coordinator/bin/tests/test_cc_invoke_py.py`,
where the spawns are counted and declared; duplicating them here would add uncounted
spawns to a shared machine for no extra coverage.

Spec backlink: pln-a-safe-target-for-transport-fa-7ea067 § C1
"""

from __future__ import annotations

import pytest

import coordinator_core.ops  # noqa: F401 -- import for side effect: registers every op
from coordinator_core import ipc
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.diagnostics_probes import (
    DiagnosticsRefusal,
    DiagnosticsStructuralPin,
    _always_refuses,
    _always_structural_pin,
    _always_succeeds,
)

_PROBE_OPS = (
    "diagnostics.always_succeeds",
    "diagnostics.always_refuses",
    "diagnostics.always_structural_pin",
)


@pytest.mark.parametrize("op_key", _PROBE_OPS)
def test_probe_is_registered(op_key: str) -> None:
    assert ipc.get_op_handler(op_key) is not None


@pytest.mark.parametrize("op_key", _PROBE_OPS)
def test_probe_has_module_map_entry(op_key: str) -> None:
    assert OP_MODULE_MAP[op_key] == "coordinator_core.ops.diagnostics_probes"


@pytest.mark.parametrize("op_key", _PROBE_OPS)
def test_probe_is_scoped_none(op_key: str) -> None:
    """"none" is the whole point: a probe that needed a repo key would touch a repo."""
    assert _OP_KEY_SCOPE[op_key] == "none"


def test_always_succeeds_returns_trivial_result() -> None:
    assert _always_succeeds({}, repo_root=None) == {
        "probe": "always_succeeds",
        "ok": True,
    }


def test_always_refuses_raises_a_non_structural_error() -> None:
    """The rc=1 rung: an ordinary exception, so dispatch maps it to INTERNAL_ERROR."""
    with pytest.raises(DiagnosticsRefusal):
        _always_refuses({}, repo_root=None)
    assert getattr(DiagnosticsRefusal, "structurally_wedged", False) is False


def test_always_structural_pin_raises_a_structurally_wedged_error() -> None:
    """The rc=2 rung: the duck-type marker ipc._handler_exception_error reads."""
    with pytest.raises(DiagnosticsStructuralPin):
        _always_structural_pin({}, repo_root=None)
    assert DiagnosticsStructuralPin.structurally_wedged is True


def test_refusal_error_maps_to_internal_error_code() -> None:
    error = ipc._handler_exception_error(DiagnosticsRefusal("boom"))
    assert error["code"] == ipc.INTERNAL_ERROR


def test_structural_pin_error_maps_to_structural_pin_code() -> None:
    error = ipc._handler_exception_error(DiagnosticsStructuralPin("boom"))
    assert error["code"] == ipc.STRUCTURAL_PIN_ERROR
    assert "DiagnosticsStructuralPin" in error["message"]


@pytest.mark.parametrize("op_key", _PROBE_OPS)
def test_probes_ignore_params_and_repo_root(op_key: str) -> None:
    """No param can steer a probe — an env/param-gated probe is not safe by construction."""
    handler = ipc.get_op_handler(op_key)
    hostile = {"path": "state/definitely-not-written.txt", "repo_root": "/nope"}
    if op_key == "diagnostics.always_succeeds":
        assert handler(hostile, repo_root=None)["ok"] is True
    else:
        with pytest.raises((DiagnosticsRefusal, DiagnosticsStructuralPin)):
            handler(hostile, repo_root=None)
