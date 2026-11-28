"""The warm boot prewarm imports every op module, so dispatch reads no module off disk."""
from __future__ import annotations

import importlib
import sys

from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.warm import server


def test_prewarm_imports_every_op_module_and_engine_drift_lazies(monkeypatch) -> None:
    imported = []
    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *a: (imported.append(name), real(name, *a))[1])
    server._prewarm_op_modules()
    assert set(OP_MODULE_MAP.values()) <= set(imported)
    assert set(server._PREWARM_ENGINE_DRIFT_MODULES) <= set(imported)
    assert all(name in sys.modules for name in server._PREWARM_ENGINE_DRIFT_MODULES)


def test_a_failing_op_module_import_is_swallowed_and_the_rest_still_load(monkeypatch) -> None:
    real = importlib.import_module
    seen = []

    def flaky(name, *a):
        seen.append(name)
        if name == sorted(set(OP_MODULE_MAP.values()))[0]:
            raise ImportError("boom")
        return real(name, *a)

    monkeypatch.setattr(importlib, "import_module", flaky)
    server._prewarm_op_modules()
    assert set(server._PREWARM_ENGINE_DRIFT_MODULES) <= set(seen)


def test_the_pool_prewarm_runs_the_op_module_prewarm(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(server, "_prewarm_guard_bodies", lambda: None)
    monkeypatch.setattr(server, "_prewarm_op_modules", lambda: called.append(True))
    server._prewarm_noop()
    assert called == [True]
