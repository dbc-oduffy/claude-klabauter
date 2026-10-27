"""Tests for the transitional patch-forwarding shim."""

from __future__ import annotations

import sys
import types

import pytest

from coordinator_core.ops.dispatch_emit import _split_forwarding as sf

OLD, NEW = "_sf_test_old", "_sf_test_new"


@pytest.fixture
def mods():
    old = types.ModuleType(OLD)
    new = types.ModuleType(NEW)
    exec("def helper():\n    return 'real'\n", old.__dict__)
    exec("def helper():\n    return 'real'\n"
         "def moved():\n    return helper()\n", new.__dict__)
    exec("def stayed():\n    return helper()\n", old.__dict__)
    sys.modules[OLD], sys.modules[NEW] = old, new
    sf.install(OLD, {"helper": [NEW], "moved": [NEW]})
    yield old, new
    sf.uninstall(OLD)
    sys.modules.pop(OLD, None)
    sys.modules.pop(NEW, None)


def test_patched_imported_name_reaches_moved_def_and_stayed_def(mods, monkeypatch):
    old, new = mods
    monkeypatch.setattr(old, "helper", lambda: "fake")
    assert new.moved() == "fake"
    assert old.stayed() == "fake"


def test_undo_restores_both_bindings():
    old = types.ModuleType(OLD)
    new = types.ModuleType(NEW)
    exec("def helper():\n    return 'real'\n", old.__dict__)
    exec("def helper():\n    return 'real'\n", new.__dict__)
    sys.modules[OLD], sys.modules[NEW] = old, new
    try:
        sf.install(OLD, {"helper": [NEW]})
        mp = pytest.MonkeyPatch()
        mp.setattr(old, "helper", lambda: "fake")
        assert new.helper() == "fake"
        mp.undo()
        assert old.helper() == "real" and new.helper() == "real"
    finally:
        sf.uninstall(OLD)
        sys.modules.pop(OLD, None)
        sys.modules.pop(NEW, None)


def test_unmapped_name_does_not_forward(mods):
    old, new = mods
    old.other = 1
    assert not hasattr(new, "other")


def test_uninstall_restores_plain_module(mods):
    old, new = mods
    sf.uninstall(OLD)
    assert type(old) is types.ModuleType
    old.helper = lambda: "x"
    assert new.helper() == "real"
