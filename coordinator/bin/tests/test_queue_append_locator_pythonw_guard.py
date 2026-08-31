"""test_queue_append_locator_pythonw_guard.py — `_resolve_python_interpreter`
must reject `pythonw`-named interpreters, not just non-python launcher exes.

Spec backlink: this session's 2026-08-31 review-integration pass on the
`interpreter` slice, code-reviewer P2 finding: `_resolve_python_interpreter`
accepted `pythonw.exe`/`pythonw3.exe` under a bare `startswith("python")`
basename test, with no distinction from `python.exe`. `pythonw` is the
GUI-subsystem build with no usable stdout by default, and this locator's
whole contract is that the resolved interpreter's child process PRINTS the
path `coordinator-queue-append` wrote — a `pythonw`-resolved interpreter
would reproduce the exact silent-loss defect class this module exists to
close.

Verified failing against the pre-fix function (bare
`os.path.basename(exe).lower().startswith("python")`, no `pythonw`
exclusion, and an early `return None` on any `sys.executable` match) before
writing these assertions:
  - test_pythonw_sys_executable_rejected_falls_through: pre-fix code returns
    `exe` itself (accepts pythonw) instead of falling through and returning
    the console fallback.
  - test_pythonw_base_executable_rejected: pre-fix code returns `base`
    itself instead of falling through to `shutil.which`.
  - test_plain_python_exe_accepted / test_non_python_launcher_exe_rejected /
    test_nothing_resolvable_returns_none: these already passed pre-fix (not
    the regression under test) and are included for full-ladder coverage so
    a future edit to the ladder cannot silently break them alongside the
    pythonw exclusion.

Run: python3 -m pytest coordinator/bin/tests/test_queue_append_locator_pythonw_guard.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import unittest.mock
from pathlib import Path

# abs-path-ok: every Windows-drive path below (C:\Python311\..., C:\venv\...,
# C:\bin\...) is a synthetic fixture value patched into sys.executable /
# sys._base_executable / shutil.which for this test only -- it never touches
# the filesystem and is not a citation of a real host path.
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_MODULE_PATH = _BIN_DIR / "_queue_append_locator.py"

_loader = importlib.machinery.SourceFileLoader("_queue_append_locator_under_test", str(_MODULE_PATH))
_spec = importlib.util.spec_from_loader("_queue_append_locator_under_test", _loader)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_mod)


def _patched(executable, base_executable, which_map):
    """Context manager patching sys.executable / sys._base_executable / shutil.which
    inside the module under test."""
    which = lambda name: which_map.get(name)  # noqa: E731
    return unittest.mock.patch.multiple(
        _mod.sys,
        executable=executable,
        _base_executable=base_executable,
    ), unittest.mock.patch.object(_mod.shutil, "which", side_effect=which)


def test_plain_python_exe_accepted():
    ctx1, ctx2 = _patched(r"C:\Python311\python.exe", None, {})
    with ctx1, ctx2:
        assert _mod._resolve_python_interpreter() == r"C:\Python311\python.exe"


def test_pythonw_sys_executable_rejected_falls_through():
    """A pythonw.exe sys.executable must not be returned as-is, but the
    ladder must fall through to a console interpreter rather than refusing."""
    ctx1, ctx2 = _patched(
        r"C:\Python311\pythonw.exe",
        None,
        {"python3": None, "python": r"C:\Python311\python.exe"},
    )
    with ctx1, ctx2:
        result = _mod._resolve_python_interpreter()
    assert result == r"C:\Python311\python.exe"
    assert result != r"C:\Python311\pythonw.exe"


def test_pythonw_base_executable_rejected():
    """pythonw3.exe on the _base_executable leg must also be rejected, with
    fallthrough to shutil.which."""
    ctx1, ctx2 = _patched(
        r"C:\venv\Scripts\some-launcher.exe",
        r"C:\Python311\pythonw3.exe",
        {"python3": r"C:\Python311\python3.exe", "python": None},
    )
    with ctx1, ctx2:
        result = _mod._resolve_python_interpreter()
    assert result == r"C:\Python311\python3.exe"


def test_non_python_launcher_exe_rejected():
    """Original defect: a forwarder .exe (sys.executable naming the
    forwarder's own embedded interpreter) must never be handed back."""
    ctx1, ctx2 = _patched(
        r"C:\bin\coordinator-lesson-add.exe",
        None,
        {"python3": None, "python": r"C:\Python311\python.exe"},
    )
    with ctx1, ctx2:
        result = _mod._resolve_python_interpreter()
    assert result == r"C:\Python311\python.exe"


def test_nothing_resolvable_returns_none():
    ctx1, ctx2 = _patched(
        r"C:\bin\coordinator-lesson-add.exe",
        None,
        {"python3": None, "python": None},
    )
    with ctx1, ctx2:
        assert _mod._resolve_python_interpreter() is None


def test_python3_dotted_version_and_venv_still_accepted():
    ctx1, ctx2 = _patched(r"C:\venv\Scripts\python.exe", None, {})
    with ctx1, ctx2:
        assert _mod._resolve_python_interpreter() == r"C:\venv\Scripts\python.exe"

    ctx1, ctx2 = _patched(r"/usr/bin/python3.13", None, {})
    with ctx1, ctx2:
        assert _mod._resolve_python_interpreter() == "/usr/bin/python3.13"
