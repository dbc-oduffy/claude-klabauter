"""The autouse source-preflight stub is a no-op when coordinator_core.percolate is absent."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_CONFTEST = Path(__file__).with_name("conftest.py")


def _stub_fn(name="_stub_source_preflight"):
    spec = importlib.util.spec_from_file_location("_bin_conftest_under_test", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fixture = getattr(mod, name)
    return getattr(fixture, "__wrapped__", None) or fixture.__pytest_wrapped__.obj


def test_absent_percolate_returns_without_raising(monkeypatch):
    fn = _stub_fn()
    monkeypatch.setitem(sys.modules, "coordinator_core.percolate", None)
    monkeypatch.delitem(sys.modules, "coordinator_core.percolate.source_preflight", raising=False)
    request = SimpleNamespace(module=SimpleNamespace())
    assert fn(request, monkeypatch) is None


def test_opt_in_real_preflight_returns_before_import(monkeypatch):
    fn = _stub_fn()
    monkeypatch.setitem(sys.modules, "coordinator_core.percolate", None)
    request = SimpleNamespace(module=SimpleNamespace(REAL_SOURCE_PREFLIGHT=True))
    assert fn(request, monkeypatch) is None


def _fake_percolate_package(tmp_path, monkeypatch, files):
    """A real `coordinator_core.percolate` package on tmp_path holding only `files`."""
    import types

    pkg_dir = tmp_path / "percolate"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        (pkg_dir / name).write_text(body, encoding="utf-8")
    pkg = types.ModuleType("coordinator_core.percolate")
    pkg.__path__ = [str(pkg_dir)]
    monkeypatch.setitem(sys.modules, "coordinator_core.percolate", pkg)
    monkeypatch.delitem(sys.modules, "coordinator_core.percolate.source_preflight", raising=False)


def test_percolate_present_without_source_preflight_returns_without_raising(tmp_path, monkeypatch):
    _fake_percolate_package(tmp_path, monkeypatch, {})
    with pytest.raises(ImportError) as shape:
        from coordinator_core.percolate import source_preflight  # noqa: F401
    assert not isinstance(shape.value, ModuleNotFoundError)
    request = SimpleNamespace(module=SimpleNamespace())
    assert _stub_fn()(request, monkeypatch) is None


def test_fold_bar_absent_with_ops_present_returns_without_raising(tmp_path, monkeypatch):
    import types

    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    ops = types.ModuleType("coordinator_core.ops")
    ops.__path__ = [str(ops_dir)]
    monkeypatch.setitem(sys.modules, "coordinator_core.ops", ops)
    monkeypatch.delitem(sys.modules, "coordinator_core.ops.fold_bar", raising=False)
    fn = _stub_fn("_stub_fold_bar_verdict")
    request = SimpleNamespace(module=SimpleNamespace(), node=SimpleNamespace(get_closest_marker=lambda _n: None))
    assert fn(request, monkeypatch) is None


def test_present_module_with_broken_transitive_import_raises(tmp_path, monkeypatch):
    _fake_percolate_package(tmp_path, monkeypatch, {"source_preflight.py": "from . import no_such_sibling\n"})
    request = SimpleNamespace(module=SimpleNamespace())
    with pytest.raises(ImportError):
        _stub_fn()(request, monkeypatch)


def test_present_module_importing_a_foreign_missing_module_raises(tmp_path, monkeypatch):
    _fake_percolate_package(tmp_path, monkeypatch, {"source_preflight.py": "import not_a_real_dep_xyz\n"})
    request = SimpleNamespace(module=SimpleNamespace())
    with pytest.raises(ModuleNotFoundError):
        _stub_fn()(request, monkeypatch)


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


def test_present_package_missing_submodule_returns(monkeypatch, tmp_path):
    import types
    package = types.ModuleType("coordinator_core.percolate")
    package.__path__ = [str(tmp_path)]
    monkeypatch.setitem(sys.modules, "coordinator_core.percolate", package)
    monkeypatch.delitem(sys.modules, "coordinator_core.percolate.source_preflight", raising=False)
    assert _stub_fn()(SimpleNamespace(module=SimpleNamespace()), monkeypatch) is None


def test_present_module_internal_import_failure_propagates(monkeypatch, tmp_path):
    import types
    package = types.ModuleType("coordinator_core.percolate")
    package.__path__ = [str(tmp_path)]
    (tmp_path / "source_preflight.py").write_text("import coordinator_core.definitely_missing_dependency\n")
    monkeypatch.setitem(sys.modules, "coordinator_core.percolate", package)
    monkeypatch.delitem(sys.modules, "coordinator_core.percolate.source_preflight", raising=False)
    with pytest.raises(ModuleNotFoundError, match="definitely_missing_dependency"):
        _stub_fn()(SimpleNamespace(module=SimpleNamespace()), monkeypatch)
