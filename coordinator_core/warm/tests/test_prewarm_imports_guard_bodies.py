"""A pool worker's prewarm imports every guard body, so a just-restarted engine's first hooks do not."""
from __future__ import annotations

import sys

from coordinator_core.warm import server


def test_prewarm_guard_bodies_imports_the_lazily_loaded_guards(monkeypatch) -> None:
    lazy = ("coordinator_core.bash_guards.dispatch_checks", "coordinator_core.bash_guards.block_subagent_commit")
    for name in lazy:
        monkeypatch.delitem(sys.modules, name, raising=False)
    server._prewarm_guard_bodies()
    assert all(name in sys.modules for name in lazy)


def test_prewarm_skips_test_modules(monkeypatch) -> None:
    import importlib

    imported = []
    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name: (imported.append(name), real(name))[1])
    server._prewarm_guard_bodies()
    assert imported and not any(".tests" in name for name in imported)


def test_the_pool_prewarm_runs_the_guard_prewarm(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(server, "_prewarm_guard_bodies", lambda: called.append(True))
    server._prewarm_noop()
    assert called == [True]
