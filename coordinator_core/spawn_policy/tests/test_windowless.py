"""Pins spawn_policy.windowless: Popen without creationflags gets CREATE_NO_WINDOW; explicit flags win."""

import subprocess
import sys

import pytest

from coordinator_core.spawn_policy import windowless

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="console windows are a Windows concern")


class _Recorded(Exception):
    pass


def _flags_after_install(monkeypatch, **kwargs):
    seen = {}

    def _record(_self, *_args, **kw):
        seen.update(kw)
        raise _Recorded

    # Install over a recorder so nothing spawns; the wrapper must forward to it.
    monkeypatch.setattr(subprocess.Popen, "__init__", _record)
    assert windowless.install() is True
    assert windowless.install() is False  # idempotent: a second root import never double-wraps
    with pytest.raises(_Recorded):
        subprocess.Popen(["git", "--version"], **kwargs)
    return seen.get("creationflags")


def test_default_spawn_gets_no_window(monkeypatch):
    assert _flags_after_install(monkeypatch) == subprocess.CREATE_NO_WINDOW


def test_explicit_creationflags_is_kept(monkeypatch):
    flags = subprocess.CREATE_NEW_CONSOLE
    assert _flags_after_install(monkeypatch, creationflags=flags) == flags


def test_both_console_less_roots_install_it():
    from pathlib import Path

    root = Path(windowless.__file__).resolve().parents[1]
    for rel in ("warm/server.py", "ops/workflow_fire/headless.py"):
        assert "windowless.install()" in (root / rel).read_text(encoding="utf-8"), rel
