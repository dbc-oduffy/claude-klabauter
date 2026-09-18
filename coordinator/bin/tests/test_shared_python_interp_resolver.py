"""test_shared_python_interp_resolver.py — oracle for the shared
`coordinator/bin/lib/python_interp.py` console-CPython resolver.

Spec backlink: docs/plans/2026-08-31-the-sys-executable-class-one-shared-inte.md,
plan-spine row C2. Modelled on the sibling
`test_queue_append_locator_pythonw_guard.py`: load the module by location,
patch `sys.executable` / `sys._base_executable` / `shutil.which` / `os.name`
with synthetic values, touch no filesystem and spawn no process.

Cases (each a sentence the prime exit criterion can be read against):
1. Forwarder-shaped `sys.executable` is refused; ladder falls through to a
   console python.
2. `pythonw.exe` / `pythonw3.exe` are refused and fall through, never
   returning None early.
3. `sys._base_executable` is consulted before `shutil.which` and accepted
   when console.
4. Store-alias preservation: a genuine `sys.executable` is returned even
   when `shutil.which("python3")` would have resolved the WindowsApps stub.
5. POSIX parity under `os.name == "posix"`.
6. Nothing resolvable -> None, and `python_argv` -> None.
7. Zero-spawn: `subprocess.run`/`subprocess.Popen` patched to raise; both
   entry points called; no raise observed.
8. Stdlib-only: module-scope imports read with `ast`, asserted a subset of
   `sys.stdlib_module_names`.

Run: python3 -m pytest coordinator/bin/tests/test_shared_python_interp_resolver.py
"""
from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import subprocess
import unittest.mock
from pathlib import Path

# abs-path-ok: every Windows-drive path below (C:\Python311\..., C:\venv\...,
# C:\bin\...) is a synthetic fixture value patched into sys.executable /
# sys._base_executable / shutil.which for this test only -- it never touches
# the filesystem and is not a citation of a real host path.
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_MODULE_PATH = _BIN_DIR / "lib" / "python_interp.py"

_loader = importlib.machinery.SourceFileLoader("_python_interp_under_test", str(_MODULE_PATH))
_spec = importlib.util.spec_from_loader("_python_interp_under_test", _loader)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_mod)


def _patched(executable, base_executable, which_map, os_name=None):
    """Context managers patching sys.executable / sys._base_executable /
    shutil.which / os.name inside the module under test."""
    which = lambda name: which_map.get(name)  # noqa: E731
    ctxs = [
        unittest.mock.patch.multiple(
            _mod.sys,
            executable=executable,
            _base_executable=base_executable,
        ),
        unittest.mock.patch.object(_mod.shutil, "which", side_effect=which),
    ]
    if os_name is not None:
        ctxs.append(unittest.mock.patch.object(_mod.os, "name", os_name))
    return ctxs


def _enter_all(ctxs):
    for ctx in ctxs:
        ctx.__enter__()


def _exit_all(ctxs):
    for ctx in reversed(ctxs):
        ctx.__exit__(None, None, None)


def test_forwarder_shaped_sys_executable_refused_falls_through():
    ctxs = _patched(
        r"C:\bin\coordinator-doc-new.exe",
        None,
        {"python3": None, "python": r"C:\Python311\python.exe"},
    )
    _enter_all(ctxs)
    try:
        result = _mod.resolve_console_python()
    finally:
        _exit_all(ctxs)
    assert result == r"C:\Python311\python.exe"
    assert result != r"C:\bin\coordinator-doc-new.exe"


def test_pythonw_sys_executable_refused_falls_through_never_none_early():
    ctxs = _patched(
        r"C:\Python311\pythonw.exe",
        None,
        {"python3": None, "python": r"C:\Python311\python.exe"},
    )
    _enter_all(ctxs)
    try:
        result = _mod.resolve_console_python()
    finally:
        _exit_all(ctxs)
    assert result == r"C:\Python311\python.exe"


def test_pythonw3_base_executable_refused_falls_through():
    ctxs = _patched(
        r"C:\bin\some-launcher.exe",
        r"C:\Python311\pythonw3.exe",
        {"python3": r"C:\Python311\python3.exe", "python": None},
    )
    _enter_all(ctxs)
    try:
        result = _mod.resolve_console_python()
    finally:
        _exit_all(ctxs)
    assert result == r"C:\Python311\python3.exe"


def test_base_executable_consulted_before_which_when_console():
    # Forward-slash Windows-drive spelling here (rather than backslash) so
    # os.path.basename parses the drive-letter path correctly when this
    # oracle itself runs on a POSIX test host -- os.path is bound to the
    # host OS at import time, not to the os.name value patched below, so a
    # backslash-separated path would be misparsed as a single opaque
    # basename component on POSIX regardless of what this test asserts
    # about the resolver's own logic.
    ctxs = _patched(
        "C:/bin/some-launcher.exe",
        "C:/Python311/python.exe",
        {"python3": "C:/SHOULD_NOT_BE_USED/python3.exe", "python": None},
    )
    _enter_all(ctxs)
    try:
        result = _mod.resolve_console_python()
    finally:
        _exit_all(ctxs)
    assert result == "C:/Python311/python.exe"


def test_store_alias_preservation_sys_executable_wins_over_which():
    """A genuine sys.executable is returned even when shutil.which("python3")
    would have resolved the WindowsApps Store-alias stub -- proves the
    Windows design intent survived the sweep.

    Forward-slash Windows-drive spelling (see
    test_base_executable_consulted_before_which_when_console for why)."""
    ctxs = _patched(
        "C:/Python311/python.exe",
        None,
        {
            "python3": "C:/Users/me/AppData/Local/Microsoft/WindowsApps/python3.exe",
            "python": "C:/Users/me/AppData/Local/Microsoft/WindowsApps/python.exe",
        },
        os_name="nt",
    )
    _enter_all(ctxs)
    try:
        result = _mod.resolve_console_python()
    finally:
        _exit_all(ctxs)
    assert result == "C:/Python311/python.exe"


def test_posix_parity():
    ctxs = _patched("/usr/bin/python3.11", None, {}, os_name="posix")
    _enter_all(ctxs)
    try:
        result = _mod.resolve_console_python()
    finally:
        _exit_all(ctxs)
    assert result == "/usr/bin/python3.11"


def test_nothing_resolvable_returns_none():
    ctxs = _patched(
        r"C:\bin\coordinator-doc-new.exe",
        None,
        {"python3": None, "python": None},
    )
    _enter_all(ctxs)
    try:
        assert _mod.resolve_console_python() is None
    finally:
        _exit_all(ctxs)


def test_python_argv_none_when_nothing_resolvable():
    ctxs = _patched(
        r"C:\bin\coordinator-doc-new.exe",
        None,
        {"python3": None, "python": None},
    )
    _enter_all(ctxs)
    try:
        assert _mod.python_argv("script.py", "--flag") is None
    finally:
        _exit_all(ctxs)


def test_python_argv_builds_expected_list():
    ctxs = _patched(r"/usr/bin/python3", None, {})
    _enter_all(ctxs)
    try:
        result = _mod.python_argv("script.py", "--flag", "value")
    finally:
        _exit_all(ctxs)
    assert result == ["/usr/bin/python3", "script.py", "--flag", "value"]


def test_zero_spawn_resolve_console_python():
    ctxs = _patched(r"C:\Python311\python.exe", None, {})
    with unittest.mock.patch.object(
        subprocess, "run", side_effect=AssertionError("resolve_console_python must not spawn")
    ), unittest.mock.patch.object(
        subprocess, "Popen", side_effect=AssertionError("resolve_console_python must not spawn")
    ):
        _enter_all(ctxs)
        try:
            _mod.resolve_console_python()
        finally:
            _exit_all(ctxs)


def test_zero_spawn_python_argv():
    ctxs = _patched(r"C:\Python311\python.exe", None, {})
    with unittest.mock.patch.object(
        subprocess, "run", side_effect=AssertionError("python_argv must not spawn")
    ), unittest.mock.patch.object(
        subprocess, "Popen", side_effect=AssertionError("python_argv must not spawn")
    ):
        _enter_all(ctxs)
        try:
            _mod.python_argv("script.py")
        finally:
            _exit_all(ctxs)


def test_stdlib_only_module_scope_imports():
    """Module-scope imports of python_interp.py are a subset of
    sys.stdlib_module_names -- read structurally with ast, never by
    timing."""
    import sys as _real_sys

    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_names.add(node.module.split(".")[0])

    assert imported_names, "expected at least one module-scope import"
    assert imported_names <= _real_sys.stdlib_module_names
    assert "subprocess" not in imported_names
