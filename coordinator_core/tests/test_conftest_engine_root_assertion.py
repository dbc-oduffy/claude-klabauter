"""The suite-root conftest refuses a coordinator_core that resolves outside the repo root."""

from __future__ import annotations

import os
import sys
import types

import pytest


def _assertion():
    for mod in list(sys.modules.values()):
        fn = getattr(mod, "_assert_engine_under_repo_root", None)
        if fn is not None and (getattr(mod, "__file__", None) or "").endswith("conftest.py"):
            return fn
    raise AssertionError("coordinator_core/conftest.py assertion not loaded")


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_live_engine_passes():
    _assertion()(REPO_ROOT)


def test_foreign_origin_fails_naming_both_paths(monkeypatch, tmp_path):
    foreign = tmp_path / "mirror" / "coordinator_core" / "__init__.py"
    fake = types.ModuleType("coordinator_core")
    fake.__file__ = str(foreign)
    monkeypatch.setitem(sys.modules, "coordinator_core", fake)
    with pytest.raises(RuntimeError) as exc:
        _assertion()(REPO_ROOT, ("coordinator_core",))
    assert str(foreign) in str(exc.value)
    assert REPO_ROOT in str(exc.value)


def test_foreign_submodule_fails(monkeypatch, tmp_path):
    foreign = tmp_path / "mirror" / "coordinator_core" / "sizing_assemble.py"
    fake = types.ModuleType("coordinator_core.sizing_assemble")
    fake.__file__ = str(foreign)
    monkeypatch.setitem(sys.modules, "coordinator_core.sizing_assemble", fake)
    with pytest.raises(RuntimeError) as exc:
        _assertion()(REPO_ROOT, ("coordinator_core.sizing_assemble",))
    assert "coordinator_core.sizing_assemble" in str(exc.value)


def test_sibling_prefix_dir_is_outside(monkeypatch):
    sibling = REPO_ROOT + "-mirror"
    fake = types.ModuleType("coordinator_core")
    fake.__file__ = os.path.join(sibling, "coordinator_core", "__init__.py")
    monkeypatch.setitem(sys.modules, "coordinator_core", fake)
    with pytest.raises(RuntimeError):
        _assertion()(REPO_ROOT, ("coordinator_core",))
