from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import unittest.mock
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_MODULE_PATH = _BIN_DIR / "_queue_append_locator.py"

_loader = importlib.machinery.SourceFileLoader("_queue_append_locator_under_test", str(_MODULE_PATH))
_spec = importlib.util.spec_from_loader("_queue_append_locator_under_test", _loader)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_mod)


def _patched(executable, base_executable, which_map):
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
    ctx1, ctx2 = _patched(
        r"C:\venv\Scripts\some-launcher.exe",
        r"C:\Python311\pythonw3.exe",
        {"python3": r"C:\Python311\python3.exe", "python": None},
    )
    with ctx1, ctx2:
        result = _mod._resolve_python_interpreter()
    assert result == r"C:\Python311\python3.exe"


def test_non_python_launcher_exe_rejected():
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
