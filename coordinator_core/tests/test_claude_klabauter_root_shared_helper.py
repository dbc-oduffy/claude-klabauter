"""
coordinator_core.tests.test_claude_klabauter_root_shared_helper -- pins the shared,
memoized ``_machine_local_get`` extracted into ``coordinator_core._claude_klabauter_root``
(R4, docs/plans/2026-09-22-spawn-budget-and-census.md, chunk C2).

Covers exactly the four cases named in that chunk's body:
  (a) two calls for the same key in one process spawn once (spawn_counter
      delta of 1).
  (b) a different key spawns again.
  (c) the timeout is passed on every spawn.
  (d) importing the module spawns 0 and registers no op.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

from coordinator_core import _claude_klabauter_root
from coordinator_core.telemetry import spawn_counter


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every case gets a clean memoization cache -- a stale entry from a
    prior case's env/subprocess result would otherwise leak forward in file
    order (module-level dict persists across tests within one process)."""
    _claude_klabauter_root.clear_machine_local_cache()
    yield
    _claude_klabauter_root.clear_machine_local_cache()


def _write_impl(tmp_path, value: str):
    impl = tmp_path / "_machine_local.py"
    impl.write_text(
        "import sys\n"
        "def main():\n"
        f"    print({value!r})\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    return impl


def test_two_calls_same_key_spawn_once(monkeypatch, tmp_path):
    """(a) two calls for the same key in one process spawn once, measured as
    a spawn_counter.spawn_count() delta of 1."""
    impl = _write_impl(tmp_path, "/some/python")
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl))

    before = spawn_counter.spawn_count()
    first = _claude_klabauter_root._machine_local_get("coordinator.python")
    second = _claude_klabauter_root._machine_local_get("coordinator.python")
    delta = spawn_counter.spawn_count() - before

    assert first == "/some/python"
    assert second == "/some/python"
    assert delta == 1


def test_different_key_spawns_again(monkeypatch, tmp_path):
    """(b) a different key spawns again -- the cache is keyed per (key, impl
    path), not per-process-wide."""
    impl = tmp_path / "_machine_local.py"
    impl.write_text(
        "import sys\n"
        "def main():\n"
        "    key = sys.argv[2]\n"
        "    print(f'value-for-{key}')\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl))

    before = spawn_counter.spawn_count()
    first = _claude_klabauter_root._machine_local_get("coordinator.python")
    second = _claude_klabauter_root._machine_local_get("repos.claude_klabauter")
    delta = spawn_counter.spawn_count() - before

    assert first == "value-for-coordinator.python"
    assert second == "value-for-repos.claude_klabauter"
    assert delta == 2


def test_timeout_passed_on_every_spawn(monkeypatch, tmp_path):
    """(c) the timeout is passed on every spawn -- one module constant, not
    three inconsistent copies (the improvement-queue gap this extraction
    closes)."""
    impl = tmp_path / "_machine_local.py"
    impl.write_text("# fake impl\n")
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl))

    seen_timeouts = []

    class _FakeCompleted:
        returncode = 0
        stdout = "/some/python\n"

    def _fake_run(cmd, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        return _FakeCompleted()

    monkeypatch.setattr(_claude_klabauter_root.subprocess, "run", _fake_run)

    _claude_klabauter_root._machine_local_get("coordinator.python")
    _claude_klabauter_root.clear_machine_local_cache()
    _claude_klabauter_root._machine_local_get("coordinator.python")

    assert seen_timeouts == [_claude_klabauter_root._MACHINE_LOCAL_TIMEOUT] * 2
    assert _claude_klabauter_root._MACHINE_LOCAL_TIMEOUT == 5


def test_import_spawns_zero_and_registers_no_op(monkeypatch):
    """(d) importing the module spawns 0 and registers no op -- no
    register_op, no I/O, no spawn at import, so a read-op module can import
    it without inheriting a write-op's side effects."""
    sys.modules.pop("coordinator_core._claude_klabauter_root", None)

    before = spawn_counter.spawn_count()
    registered = []
    monkeypatch.setattr(
        "coordinator_core.ipc.register_op",
        lambda *a, **k: registered.append((a, k)),
        raising=False,
    )

    module = importlib.import_module("coordinator_core._claude_klabauter_root")

    assert spawn_counter.spawn_count() - before == 0
    assert registered == []
    assert hasattr(module, "_machine_local_get")


def test_module_free_of_import_time_register_op_call():
    """Static (AST) check the module source contains no `register_op(...)`
    call anywhere -- belt-and-suspenders alongside the dynamic import test
    above. AST-based rather than a text search so the module's own prose
    docstrings (which discuss `register_op()` as the thing this module must
    NOT do) cannot trip a naive substring match."""
    import ast
    import inspect

    source = inspect.getsource(_claude_klabauter_root)
    tree = ast.parse(source)
    call_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "register_op" not in call_names


def test_timeout_expired_treated_as_unavailable(monkeypatch, tmp_path):
    """A hung registry script must not block indefinitely -- TimeoutExpired
    is treated the same as any other failure (None, memoized)."""
    impl = tmp_path / "_machine_local.py"
    impl.write_text("# fake impl\n")
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl))

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(_claude_klabauter_root.subprocess, "run", _fake_run)

    assert _claude_klabauter_root._machine_local_get("coordinator.python") is None
