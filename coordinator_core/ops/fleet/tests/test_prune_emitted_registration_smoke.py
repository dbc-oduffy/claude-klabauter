"""Registration smoke: fleet.prune_emitted_output is live after eager import."""

from __future__ import annotations


def test_op_registers_at_eager_import() -> None:
    from coordinator_core.ipc import _REGISTRY
    from coordinator_core.ops import _EAGER_OP_MODULES, _eager_import_all

    _eager_import_all()

    assert "fleet.prune_emitted_output" in _REGISTRY
    from coordinator_core.ops.fleet import prune_emitted as m

    assert _REGISTRY["fleet.prune_emitted_output"] is m._handler
    assert any(
        mod == "coordinator_core.ops.fleet.prune_emitted"
        for mod, _ in _EAGER_OP_MODULES
    )
