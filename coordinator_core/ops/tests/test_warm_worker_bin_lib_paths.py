"""A warm worker's restored `sys.path` never strands a real bin entry point's `<bin>/lib` imports.

Worker state at fault: `lib` cached in `sys.modules`, `coordinator/bin` and `coordinator/bin/lib` off
`sys.path`, the sibling modules (`python_interp`, `entry_point_shim`) not yet imported. Each path below
runs through its real entry point at default arguments against the real `coordinator/bin`.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import invoke_from_argv as ifa

_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
_LIB = _BIN / "lib"
_SIBLINGS = (
    "lib",
    "python_interp",
    "entry_point_shim",
    "memo_compose",
    "machine_local_impl_resolve",
    "dr_allocator",
)


@pytest.fixture
def warm_worker_state(monkeypatch):
    """`lib` imported once, then `sys.path` restored without the bin dirs; siblings uncached."""
    for mod in _SIBLINGS:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(sys, "path", [str(_BIN)] + list(sys.path))
    import lib  # noqa: F401 -- the worker's first `import lib`

    for mod in _SIBLINGS[1:]:
        sys.modules.pop(mod, None)
    sys.path[:] = [p for p in sys.path if p not in (str(_BIN), str(_LIB))]
    yield
    for mod in _SIBLINGS:
        sys.modules.pop(mod, None)


def test_mint_baton_bootstrap_resolves_python_interp(warm_worker_state, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_gate

    doc_new = ask_gate._load_doc_new()
    monkeypatch.delitem(sys.modules, "coordinator_doc_new_for_ask_gate", raising=False)

    doc_new._bootstrap_engine()  # the first call of `mint_baton_from_sizing`

    assert "python_interp" in sys.modules


def test_sizing_assemble_from_argv_resolves_entry_point_shim(warm_worker_state, tmp_path):
    result = ifa._invoke_from_argv({"argv": ["--help"], "cwd": str(tmp_path), "entrypoint": "sizing-assemble"})

    assert "ModuleNotFoundError" not in result["stderr"], result["stderr"]
    assert result["exit_code"] == 0, result["stderr"]
    assert "entry_point_shim" in sys.modules


def test_engine_ask_hook_console_python_resolves_python_interp(warm_worker_state):
    """The hook reaches `python_interp` through `resolve_validation_cmd._shared_console_python`, which
    maps an import failure to None; a stranded `<bin>/lib` would silently degrade the ladder."""
    from coordinator_core import resolve_validation_cmd

    interp = resolve_validation_cmd._shared_console_python()

    assert "python_interp" in sys.modules
    assert interp
