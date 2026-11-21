"""Every child a claude-klabauter process spawns on Windows is windowless and raises no busy cursor."""
from __future__ import annotations

import subprocess
import sys

import pytest

import coordinator_core  # noqa: F401 -- installs the default

pytestmark = [pytest.mark.skipif(sys.platform != "win32", reason="Windows spawn flags"), pytest.mark.spawns_process, pytest.mark.cadence]


def _spawn_kwargs(monkeypatch, **kwargs) -> dict:
    seen: dict = {}
    real = subprocess._winapi.CreateProcess

    def spy(exe, args, pa, ta, inherit, flags, env, cwd, startupinfo):
        seen.update(flags=flags, startupinfo=startupinfo)
        return real(exe, args, pa, ta, inherit, flags, env, cwd, startupinfo)

    monkeypatch.setattr(subprocess._winapi, "CreateProcess", spy)
    subprocess.run(["git", "--version"], capture_output=True, **kwargs)
    return seen


def test_a_bare_spawn_is_windowless_and_cursor_quiet(monkeypatch):
    seen = _spawn_kwargs(monkeypatch)
    assert seen["flags"] & subprocess.CREATE_NO_WINDOW
    assert seen["startupinfo"].dwFlags & 0x80


def test_a_spawn_asking_for_its_own_console_keeps_it(monkeypatch):
    seen = _spawn_kwargs(monkeypatch, creationflags=subprocess.CREATE_NEW_CONSOLE)
    assert not seen["flags"] & subprocess.CREATE_NO_WINDOW
