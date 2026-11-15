"""The overlay's pytest never runs windowless: a pythonw pytest hands its children invalid std handles."""
from __future__ import annotations

import sys

import pytest
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov


def _no_pin(monkeypatch):
    monkeypatch.setattr(cov, "resolve_targeted_test_python", lambda root: SimpleNamespace(command=None, defect=None))


# Trap: _console_sibling joins with ntpath, so on a POSIX host the sibling is a backslash path that never exists.
@pytest.mark.skipif(sys.platform != "win32", reason="pythonw/python.exe siblings exist only on Windows")
def test_a_windowless_engine_interpreter_runs_the_overlay_under_its_console_sibling(tmp_path, monkeypatch):
    install = tmp_path / "Python313"
    install.mkdir()
    (install / "python.exe").write_bytes(b"")
    (install / "pythonw.exe").write_bytes(b"")
    _no_pin(monkeypatch)
    monkeypatch.setattr(sys, "executable", str(install / "pythonw.exe"))

    assert cov._gate_python(tmp_path).lower() == str(install / "python.exe").lower()


def test_a_console_engine_interpreter_is_used_as_is(tmp_path, monkeypatch):
    _no_pin(monkeypatch)
    assert cov._gate_python(tmp_path) == sys.executable


def test_a_resolved_pin_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cov, "resolve_targeted_test_python", lambda root: SimpleNamespace(command="C:/pinned/python.exe", defect=None)
    )
    assert cov._gate_python(tmp_path) == "C:/pinned/python.exe"
