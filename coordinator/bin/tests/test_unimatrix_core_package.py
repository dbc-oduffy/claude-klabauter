"""Tests for the shared stdlib-only coordinator/bin/_unimatrix/ core package."""
import builtins
import importlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = BIN_DIR / "_unimatrix"


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN_DIR / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_core_package_modules_are_inert_and_stdlib_only(monkeypatch):
    def trip(what):
        def _boom(*a, **k):
            raise AssertionError(f"import side effect: {what}")
        return _boom

    monkeypatch.setattr(subprocess, "run", trip("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", trip("subprocess.Popen"))
    monkeypatch.setattr(re, "compile", trip("re.compile"))
    monkeypatch.setattr(Path, "resolve", trip("Path.resolve"))

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))

    for mod_name in (
        "_unimatrix",
        "_unimatrix.paths",
        "_unimatrix.transport",
        "_unimatrix.protocol",
        "_unimatrix.proc",
        "_unimatrix.adapters",
        "_unimatrix.adapters.base",
        "_unimatrix.adapters.claude",
        "_unimatrix.adapters.codex",
        "_unimatrix.adapters.agy",
        "_unimatrix.adapters.grok",
    ):
        sys.modules.pop(mod_name, None)
        mod = importlib.import_module(mod_name)
        assert mod is not None
        assert "coordinator_core" not in sys.modules or mod.__name__.startswith("_unimatrix")


def test_proc_parent_pid_is_spawn_free_and_matches_os_getppid(monkeypatch):
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    from _unimatrix import proc

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("parent_pid must not spawn subprocesses"))
    got = proc.parent_pid(os.getpid())
    assert got == os.getppid()


def test_host_adapters_satisfy_protocol():
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    from _unimatrix.adapters.agy import AgyAdapter
    from _unimatrix.adapters.base import HostAdapter
    from _unimatrix.adapters.claude import ClaudeAdapter
    from _unimatrix.adapters.codex import CodexAdapter
    from _unimatrix.adapters.grok import GrokAdapter

    for cls, vendor in (
        (ClaudeAdapter, "claude"),
        (CodexAdapter, "codex"),
        (AgyAdapter, "agy"),
        (GrokAdapter, "grok"),
    ):
        inst = cls()
        assert isinstance(inst, HostAdapter)
        assert inst.vendor == vendor


def test_install_scripts_vendors_core_package_and_isolated_import_works(tmp_path):
    install = _load("unimatrix-install.py")
    dest = tmp_path / "unimatrix"
    install.install_scripts(BIN_DIR, dest)
    assert (dest / "_unimatrix" / "__init__.py").is_file()
    assert (dest / "_unimatrix" / "paths.py").is_file()
    assert (dest / "_unimatrix" / "proc.py").is_file()
    assert (dest / "_unimatrix" / "transport.py").is_file()
    assert (dest / "_unimatrix" / "protocol.py").is_file()
    assert (dest / "_unimatrix" / "adapters" / "grok.py").is_file()

    # Verify isolated vendored script runs cleanly with only dest on sys.path (no repo root).
    res = subprocess.run(
        [sys.executable, str(dest / "unimatrix-peer-contract.py"), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0
    assert "unimatrix-peer-contract" in res.stdout
