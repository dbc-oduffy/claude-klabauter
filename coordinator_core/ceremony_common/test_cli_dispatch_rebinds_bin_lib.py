"""An in-process bin CLI's bootstrap resolves its `<bin>/lib` siblings on every call in a worker.

Example-retrieval-repo-82, 2026-11-11: `ModuleNotFoundError: python_interp` at coordinator-doc-new
`_bootstrap_engine`. A warm worker had `lib` cached with `coordinator/bin/lib` restored off
sys.path, and `baton_assemble.apply` runs doc-new through `invoke_cli_main`, which is outside the
served-entrypoint re-bind (`invoke_from_argv._run_entrypoint`).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

from coordinator_core.ceremony_common.cli_dispatch import invoke_cli_main

_BIN = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
_SIBLINGS = ("lib", "python_interp", "memo_compose", "machine_local_impl_resolve", "dr_allocator")


def _load_doc_new(name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(_BIN / "coordinator-doc-new.py"))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def test_doc_new_bootstrap_with_lib_cached_and_its_dir_off_sys_path(monkeypatch):
    for mod in _SIBLINGS:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(sys, "path", [str(_BIN)] + list(sys.path))
    import lib  # noqa: F401 -- the worker's first `import lib`
    sys.modules.pop("python_interp", None)
    # The worker state at fault: `lib` cached, its directory restored off sys.path.
    sys.path[:] = [p for p in sys.path if p not in (str(_BIN), str(_BIN / "lib"))]
    module = _load_doc_new("_doc_new_rebind_probe")
    monkeypatch.delitem(sys.modules, "_doc_new_rebind_probe", raising=False)

    exit_code, _out, err, _cls = invoke_cli_main(module, ["--help"])

    assert "ModuleNotFoundError" not in err, err
    assert exit_code == 0, err
    assert "python_interp" in sys.modules
    assert str(_BIN / "lib") not in sys.path, "the per-call bind must not outlive the call"


def test_doc_new_direct_bootstrap_with_lib_cached_and_its_dir_off_sys_path(monkeypatch):
    """`dispatch.emit -> mint_baton_from_sizing` calls `_bootstrap_engine` directly, outside both
    `invoke_cli_main` and the served-entrypoint re-bind (example-game-repo-19, 2026-11-11 05:55Z)."""
    for mod in _SIBLINGS:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(sys, "path", [str(_BIN)] + list(sys.path))
    import lib  # noqa: F401
    sys.modules.pop("python_interp", None)
    sys.path[:] = [p for p in sys.path if p not in (str(_BIN), str(_BIN / "lib"))]
    module = _load_doc_new("_doc_new_direct_probe")
    monkeypatch.delitem(sys.modules, "_doc_new_direct_probe", raising=False)

    module._bootstrap_engine()

    assert "python_interp" in sys.modules
