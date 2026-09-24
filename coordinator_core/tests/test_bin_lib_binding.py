"""Unit tests for coordinator_core.bin_lib_binding — see its module
docstring for the two-root invariant this leaf enforces."""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from coordinator_core import bin_lib_binding


@pytest.fixture(autouse=True)
def _isolate_sys_path_and_modules():
    """The engine bin is already bound by the root conftest by the time
    tests run, so remove it here and restore both `sys.path` and
    `sys.modules["lib"]` exactly, in `finally`."""
    saved_path = list(sys.path)
    saved_lib = sys.modules.get("lib")
    engine_bin = bin_lib_binding._ENGINE_BIN_DIR
    while engine_bin in sys.path:
        sys.path.remove(engine_bin)
    sys.modules.pop("lib", None)
    try:
        yield
    finally:
        sys.path[:] = saved_path
        if saved_lib is None:
            sys.modules.pop("lib", None)
        else:
            sys.modules["lib"] = saved_lib


def test_engine_bin_is_inserted_once_and_idempotent():
    engine_bin = bin_lib_binding._ENGINE_BIN_DIR
    assert bin_lib_binding.ensure_bin_lib_bound(engine_bin) is True
    assert sys.path.count(engine_bin) == 1
    assert bin_lib_binding.ensure_bin_lib_bound(engine_bin) is True
    assert sys.path.count(engine_bin) == 1


def test_a_correctly_bound_lib_is_left_alone():
    engine_bin = bin_lib_binding._ENGINE_BIN_DIR
    bin_lib_binding.ensure_bin_lib_bound(engine_bin)
    import lib as _lib  # noqa: PLC0415 - binds coordinator/bin/lib for this test

    bound_before = sys.modules["lib"]
    assert bin_lib_binding.ensure_bin_lib_bound(engine_bin) is True
    assert sys.modules["lib"] is bound_before
    del _lib


def test_a_foreign_namespace_lib_is_evicted_and_a_following_import_binds_ours(tmp_path):
    foreign_lib_dir = tmp_path / "lib"
    foreign_lib_dir.mkdir()
    # A namespace package -- no __init__.py -- reproduces the pywin32 shape
    # this leaf's docstring names, without depending on pywin32. Built
    # directly (a bare `find_spec("lib")` would merge in any OTHER "lib"
    # namespace portion already reachable on the ambient sys.path) so this
    # module's __path__ names only the tmp dir.
    from types import ModuleType as _ModuleType

    foreign_module = _ModuleType("lib")
    foreign_module.__path__ = [str(foreign_lib_dir)]
    sys.modules["lib"] = foreign_module

    engine_bin = bin_lib_binding._ENGINE_BIN_DIR
    assert bin_lib_binding.ensure_bin_lib_bound(engine_bin) is True
    assert "lib" not in sys.modules

    import lib as _lib  # noqa: PLC0415 - must now bind coordinator/bin/lib

    ours = os.path.normcase(os.path.abspath(os.path.join(engine_bin, "lib")))
    bound_paths = [os.path.normcase(os.path.abspath(p)) for p in _lib.__path__]
    assert ours in bound_paths
    del _lib


def test_an_unset_lib_module_is_a_no_op_beyond_the_path_insert():
    assert "lib" not in sys.modules
    engine_bin = bin_lib_binding._ENGINE_BIN_DIR
    assert bin_lib_binding.ensure_bin_lib_bound(engine_bin) is True
    assert engine_bin in sys.path
    assert "lib" not in sys.modules


def test_a_foreign_bin_dir_is_never_durably_bound(tmp_path):
    other_bin = tmp_path / "other_bin"
    other_bin.mkdir()
    engine_bin = bin_lib_binding._ENGINE_BIN_DIR

    bin_lib_binding.ensure_bin_lib_bound(engine_bin)
    path_before = list(sys.path)
    modules_lib_before = sys.modules.get("lib")

    assert bin_lib_binding.ensure_bin_lib_bound(str(other_bin)) is False
    assert sys.path == path_before
    assert sys.modules.get("lib") is modules_lib_before
    assert str(other_bin) not in sys.path

    # A following bind of the engine bin is still a no-op.
    assert bin_lib_binding.ensure_bin_lib_bound(engine_bin) is True
    assert sys.path == path_before


def test_exec_module_bin_bound_runs_a_tmp_bin_dir_module_transiently(tmp_path):
    other_bin = tmp_path / "other_bin"
    other_bin.mkdir()
    script = other_bin / "probe_script.py"
    script.write_text("import sys\nSAW_BIN_ON_PATH = str(sys.path[0])\n")

    spec = importlib.util.spec_from_file_location("probe_script", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    path_before = list(sys.path)
    bin_lib_binding.exec_module_bin_bound(spec.loader, module, str(other_bin))
    assert module.SAW_BIN_ON_PATH == str(other_bin)
    assert sys.path == path_before
    assert str(other_bin) not in sys.path
