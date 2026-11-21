"""A registry miss is answered without importing every op module on the request thread."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
import coordinator_core.ops as ops_pkg

_ROOT = str(Path(__file__).resolve().parents[2])

_MAP_COVERS_REGISTRY = textwrap.dedent(
    """
    import coordinator_core.ipc as ipc
    import coordinator_core.hooks as hooks_pkg
    import coordinator_core.ops as ops_pkg
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    ops_pkg._eager_import_all()
    hooks_pkg._eager_import_all()
    print("\\n".join(sorted(set(ipc._REGISTRY) - set(OP_MODULE_MAP))))
    """
)


_HOOK_ENTRIES_REGISTER = textwrap.dedent(
    """
    import importlib
    import coordinator_core.ipc as ipc
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    for op, mod in sorted(OP_MODULE_MAP.items()):
        if op.startswith("hooks."):
            importlib.import_module(mod)
            if op not in ipc._REGISTRY:
                print(op)
    """
)


def _boom(*_a, **_k):
    raise AssertionError("_eager_import_all ran on the request path")


def test_an_unmapped_op_miss_returns_none_without_the_whole_package_import(monkeypatch):
    monkeypatch.setattr(ops_pkg, "_eager_import_all", _boom)
    assert ipc._lazy_import_and_lookup("no.such.op.anywhere") is None
    assert ipc.get_op_handler("no.such.op.anywhere") is None


def test_a_stale_map_entry_returns_none_without_the_whole_package_import(monkeypatch):
    from coordinator_core.ops import _registry_map

    monkeypatch.setattr(ops_pkg, "_eager_import_all", _boom)
    monkeypatch.setitem(_registry_map.OP_MODULE_MAP, "stale.op.fixture", "json")
    assert ipc._lazy_import_and_lookup("stale.op.fixture") is None


def test_a_mapped_op_is_still_resolved_by_its_targeted_import(monkeypatch):
    monkeypatch.setattr(ops_pkg, "_eager_import_all", _boom)
    assert ipc._lazy_import_and_lookup("ping") is not None


def test_a_mapped_module_import_error_is_recorded_as_poisoned_and_answers_none(monkeypatch):
    from coordinator_core.ops import _registry_map

    mod = "coordinator_core_no_such_module_for_poison_pin"
    monkeypatch.setattr(ops_pkg, "_eager_import_all", _boom)
    monkeypatch.setitem(_registry_map.OP_MODULE_MAP, "poison.op.fixture", mod)
    monkeypatch.delitem(ops_pkg._POISONED_MODULES, mod, raising=False)
    try:
        assert ipc._lazy_import_and_lookup("poison.op.fixture") is None
        assert isinstance(ops_pkg.get_poisoned_modules().get(mod), ImportError)
    finally:
        ops_pkg._POISONED_MODULES.pop(mod, None)
