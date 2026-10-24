"""The autouse source-preflight stub is a no-op when coordinator_core.percolate is absent."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_CONFTEST = Path(__file__).with_name("conftest.py")


def _stub_fn():
    spec = importlib.util.spec_from_file_location("_bin_conftest_under_test", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fixture = mod._stub_source_preflight
    return getattr(fixture, "__wrapped__", None) or fixture.__pytest_wrapped__.obj


def test_absent_percolate_returns_without_raising(monkeypatch):
    fn = _stub_fn()
    monkeypatch.setitem(sys.modules, "coordinator_core.percolate", None)
    request = SimpleNamespace(module=SimpleNamespace())
    assert fn(request, monkeypatch) is None


def test_opt_in_real_preflight_returns_before_import(monkeypatch):
    fn = _stub_fn()
    monkeypatch.setitem(sys.modules, "coordinator_core.percolate", None)
    request = SimpleNamespace(module=SimpleNamespace(REAL_SOURCE_PREFLIGHT=True))
    assert fn(request, monkeypatch) is None


def test_present_percolate_is_stubbed(monkeypatch):
    pytest.importorskip("coordinator_core.percolate.source_preflight")
    from coordinator_core.percolate import source_preflight

    def original(*_a, **_k):
        raise AssertionError("real check_source_published reached")

    monkeypatch.setattr(source_preflight, "check_source_published", original)
    fn = _stub_fn()
    request = SimpleNamespace(module=SimpleNamespace())
    fn(request, monkeypatch)
    assert source_preflight.check_source_published is not original
    assert source_preflight.check_source_published("x") is None
