"""`lib/win_portability.py`'s `no_console_creationflags()` returns the same shape on both platforms."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_LIB_PATH = Path(__file__).resolve().parents[2] / "lib" / "win_portability.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module_under_test = _load_module(_LIB_PATH, "test_lib_win_portability")


def test_windows_returns_single_truthy_creationflags_key(monkeypatch):
    monkeypatch.setattr(module_under_test.os, "name", "nt")
    result = module_under_test.no_console_creationflags()
    assert list(result.keys()) == ["creationflags"]
    assert result["creationflags"]


def test_non_windows_returns_exactly_empty_dict(monkeypatch):
    monkeypatch.setattr(module_under_test.os, "name", "posix")
    result = module_under_test.no_console_creationflags()
    assert result == {}
    assert len(result) == 0


def test_spreadable_into_popen_kwargs_on_windows(monkeypatch):
    monkeypatch.setattr(module_under_test.os, "name", "nt")
    kwargs = {"stdout": subprocess.PIPE, **module_under_test.no_console_creationflags()}
    assert kwargs["stdout"] == subprocess.PIPE
    assert "creationflags" in kwargs


def test_spreadable_into_popen_kwargs_off_windows(monkeypatch):
    monkeypatch.setattr(module_under_test.os, "name", "posix")
    kwargs = {"stdout": subprocess.PIPE, **module_under_test.no_console_creationflags()}
    assert kwargs == {"stdout": subprocess.PIPE}
    assert "creationflags" not in kwargs
