"""Pins the root conftest's Windows default: a child spawned without creationflags gets CREATE_NO_WINDOW."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="console windows are a Windows concern")


class _Recorded(Exception):
    pass


def _spawn_flags(monkeypatch, **kwargs):
    # Swap the real Popen.__init__ the root conftest wraps for a recorder, so nothing spawns.
    wrapper_globals = subprocess.Popen.__init__.__globals__
    assert "_popen_init" in wrapper_globals, "root conftest no longer wraps Popen.__init__"
    seen = {}

    def _record(_self, *_args, **kw):
        seen.update(kw)
        raise _Recorded

    monkeypatch.setitem(wrapper_globals, "_popen_init", _record)
    with pytest.raises(_Recorded):
        subprocess.Popen(["git", "--version"], **kwargs)
    return seen.get("creationflags")


def test_child_without_creationflags_gets_no_window(monkeypatch):
    assert _spawn_flags(monkeypatch) == subprocess.CREATE_NO_WINDOW


def test_explicit_creationflags_is_kept(monkeypatch):
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    assert _spawn_flags(monkeypatch, creationflags=flags) == flags
