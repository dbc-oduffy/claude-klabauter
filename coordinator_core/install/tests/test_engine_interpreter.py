"""Behavior pins for coordinator_core.install.engine_interpreter."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.install import engine_interpreter as ei


def test_dir_is_under_given_home(tmp_path):
    d = ei.engine_interpreter_dir(tmp_path)
    assert d.parent == tmp_path
    assert ".coordinator-venv" not in str(d)


def test_dir_defaults_to_settings_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ei, "settings_home", lambda: tmp_path)
    assert ei.engine_interpreter_dir().parent == tmp_path


def test_interpreter_path_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(ei, "_is_windows", lambda: False)
    assert ei.interpreter_path(tmp_path) == tmp_path / "bin" / "python"
    monkeypatch.setattr(ei, "_is_windows", lambda: True)
    assert ei.interpreter_path(tmp_path) == tmp_path / "Scripts" / "python.exe"


def test_stamped_none_when_absent_and_zero_spawn(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    assert ei.stamped(tmp_path) is None


def test_stamped_returns_path_when_executable(tmp_path):
    py = ei.interpreter_path(ei.engine_interpreter_dir(tmp_path))
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    assert ei.stamped(tmp_path) == str(py)


@pytest.mark.skipif(os.name == "nt", reason="posix exec bit")
def test_stamped_refuses_non_executable(tmp_path):
    py = ei.interpreter_path(ei.engine_interpreter_dir(tmp_path))
    py.parent.mkdir(parents=True)
    py.write_text("x")
    py.chmod(0o644)
    assert ei.stamped(tmp_path) is None


def test_ensure_reuses_existing_with_zero_spawn(tmp_path, monkeypatch):
    py = ei.interpreter_path(ei.engine_interpreter_dir(tmp_path))
    py.parent.mkdir(parents=True)
    py.write_text("x")
    py.chmod(0o755)

    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    assert ei.ensure("python3", tmp_path) == str(py)


def test_ensure_creates_real_venv_with_one_spawn(tmp_path, monkeypatch):
    calls = []
    real = subprocess.run

    def counting(*a, **k):
        calls.append(a[0])
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "run", counting)
    out = ei.ensure(sys.executable, tmp_path)
    assert len(calls) == 1
    assert calls[0][1:3] == ["-m", "venv"]
    assert Path(out).is_file()
    assert ei.stamped(tmp_path) == out
    assert ei.ensure(sys.executable, tmp_path) == out
    assert len(calls) == 1


def test_ensure_raises_when_creation_fails(tmp_path):
    with pytest.raises(RuntimeError):
        ei.ensure(str(tmp_path / "no-such-python"), tmp_path)
    assert ei.stamped(tmp_path) is None
