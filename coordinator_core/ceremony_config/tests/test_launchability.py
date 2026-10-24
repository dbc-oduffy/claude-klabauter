"""launchability.check: the win32 suffix rule, the POSIX executable bit, and unresolved names."""
from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.ceremony_config import launchability


def test_unresolved_name(monkeypatch):
    monkeypatch.setattr(launchability.shutil, "which", lambda _n: None)
    assert launchability.check(["no-such-tool"]) == "not found on PATH"


def test_empty_argv():
    assert launchability.check([]) == "empty argv"


def test_win32_cmd_shim_not_launchable(monkeypatch, tmp_path):
    shim = tmp_path / "pnpm.CMD"
    shim.write_text("@echo off\n")
    monkeypatch.setattr(launchability.shutil, "which", lambda _n: str(shim))
    reason = launchability.check(["pnpm", "x"], platform="win32")
    assert reason is not None and ".exe/.com" in reason


def test_win32_extensionless_not_launchable(monkeypatch, tmp_path):
    shim = tmp_path / "pnpm"
    shim.write_text("#!/bin/sh\n")
    monkeypatch.setattr(launchability.shutil, "which", lambda _n: str(shim))
    assert launchability.check(["pnpm"], platform="win32") is not None


@pytest.mark.parametrize("name", ["tool.exe", "tool.EXE", "tool.com"])
def test_win32_exe_and_com_launchable(monkeypatch, tmp_path, name):
    exe = tmp_path / name
    exe.write_text("x")
    monkeypatch.setattr(launchability.shutil, "which", lambda _n: str(exe))
    assert launchability.check(["tool"], platform="win32") is None


def test_win32_path_argv0_checked_as_given(tmp_path):
    cmd = tmp_path / "run.bat"
    cmd.write_text("x")
    assert launchability.check([str(cmd)], platform="win32") is not None
    exe = tmp_path / "run.exe"
    exe.write_text("x")
    assert launchability.check([str(exe)], platform="win32") is None


def test_win32_suffixless_path_resolves_to_exe(tmp_path):
    (tmp_path / "hook.exe").write_text("x")
    given = str(tmp_path / "hook")
    assert launchability.resolve(given, platform="win32") == given + ".exe"
    assert launchability.check([given], platform="win32") is None
    assert launchability.resolve(given, platform="linux") is None


def test_win32_suffixless_path_without_exe_still_refused(tmp_path):
    (tmp_path / "hook.cmd").write_text("x")
    given = str(tmp_path / "hook")
    assert launchability.check([given], platform="win32") == "not found on PATH"
    (tmp_path / "hook").write_text("#!/bin/sh\n")
    assert "only .exe/.com" in launchability.check([given], platform="win32")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable bit")
def test_posix_executable_bit(tmp_path):
    f = tmp_path / "tool"
    f.write_text("#!/bin/sh\n")
    os.chmod(f, 0o644)
    assert launchability.check([str(f)], platform="linux") is not None
    os.chmod(f, 0o755)
    assert launchability.check([str(f)], platform="linux") is None
