"""The Popen default that hides child consoles must not strand a console spawner's TUI child."""
import subprocess
import sys

import pytest

import coordinator_core

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows console semantics")


def _spawn_kwargs(monkeypatch, has_console):
    seen = {}

    def recorder(self, *args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", recorder)
    monkeypatch.setattr(coordinator_core, "_get_console_window", lambda: 1 if has_console else 0)
    coordinator_core._hide_child_consoles()
    subprocess.Popen(["agy"])
    return seen


def test_a_console_spawner_lets_the_child_inherit_its_console(monkeypatch):
    kw = _spawn_kwargs(monkeypatch, has_console=True)
    assert not kw.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
    info = kw["startupinfo"]
    assert not info.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert info.dwFlags & 0x80  # STARTF_FORCEOFFFEEDBACK stays: no AppStarting cursor


def test_a_windowless_spawner_still_hides_the_child_console(monkeypatch):
    kw = _spawn_kwargs(monkeypatch, has_console=False)
    assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
    info = kw["startupinfo"]
    assert info.dwFlags & subprocess.STARTF_USESHOWWINDOW and info.wShowWindow == 0
