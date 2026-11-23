"""The overlay's pytest never runs windowless: a pythonw pytest hands its children invalid std handles."""
from __future__ import annotations

import sys

import pytest
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from pathlib import Path


def _no_pin(monkeypatch, imports_pytest=lambda cmd: True) -> None:
    monkeypatch.setattr(cov, "resolve_test_runner_python", lambda root: SimpleNamespace(command=None, defect=None))
    monkeypatch.setattr(cov, "_imports_pytest", imports_pytest)


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


def test_a_console_engine_interpreter_is_used_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_pin(monkeypatch)
    assert cov._gate_python(tmp_path) == sys.executable


def test_a_resolved_pin_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cov, "resolve_test_runner_python", lambda root: SimpleNamespace(command="C:/pinned/python.exe", defect=None)
    )
    assert cov._gate_python(tmp_path) == "C:/pinned/python.exe"


def test_an_unpinned_repo_skips_an_engine_interpreter_without_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # example-retrieval-repo run 8e2bc1: unpinned, the engine's Python312 had no pytest and the reverify leg ran zero tests.
    _no_pin(monkeypatch, imports_pytest=lambda cmd: cmd == "C:/other/python.exe")
    monkeypatch.setattr(cov, "_fallback_candidates", lambda: [sys.executable, "C:/other/python.exe"])
    assert cov._gate_python(tmp_path) == "C:/other/python.exe"


def test_an_unpinned_repo_with_no_pytest_anywhere_keeps_the_engine_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_pin(monkeypatch, imports_pytest=lambda cmd: False)
    monkeypatch.setattr(cov, "_fallback_candidates", lambda: ["C:/other/python.exe"])
    assert cov._gate_python(tmp_path) == sys.executable
