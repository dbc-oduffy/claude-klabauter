"""spawn_depth: depth sentinel, child env, self-inclusion refusal, root conftest exit. In-process only."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.testing import spawn_depth as sd

ROOT_CONFTEST = Path(__file__).resolve().parents[2] / "conftest.py"


def _norm(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


# --- depth counting -------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [(None, 0), ("", 0), ("abc", 0), ("-3", 0), ("0", 0), ("1", 1), ("2", 2)])
def test_current_depth_absent_or_garbage_is_zero(raw, expected):
    env = {} if raw is None else {sd.ENV_KEY: raw}
    assert sd.current_depth(env) == expected


def test_spawn_allowed_at_depth_zero_refused_at_one_and_above():
    assert sd.refusal_for_spawn({}) is None
    assert sd.refusal_for_spawn({sd.ENV_KEY: "0"}) is None
    for depth in ("1", "2", "40"):
        msg = sd.refusal_for_spawn({sd.ENV_KEY: depth})
        assert msg and str(int(depth) + 1) in msg and sd.ENV_KEY in msg


def test_top_level_session_may_spawn_one_child_and_child_session_runs():
    top = {sd.ENV_KEY: "0"}
    assert sd.refusal_for_spawn(top) is None
    child = sd.child_env(top)
    assert sd.session_depth(child) == 1 <= sd.MAX_DEPTH  # the depth-1 child session runs
    grand = sd.refusal_for_spawn(child)
    assert grand and "depth 2" in grand


def test_xdist_style_worker_env_is_not_counted_as_a_hop():
    worker = {sd.ENV_KEY: "0"}  # new pid, no PYTEST_CURRENT_TEST
    assert sd.session_depth(worker) == 0


def test_child_env_increments_and_strips_current_test_without_mutating_input():
    env = {sd.ENV_KEY: "0", sd.CURRENT_TEST_KEY: "a/test_x.py::t (call)", "KEEP": "1"}
    child = sd.child_env(env)
    assert child[sd.ENV_KEY] == "1"
    assert sd.CURRENT_TEST_KEY not in child
    assert child["KEEP"] == "1"
    assert env[sd.ENV_KEY] == "0" and sd.CURRENT_TEST_KEY in env


def test_launcher_outside_any_session_stamps_a_top_level_child_at_zero():
    assert sd.child_env({})[sd.ENV_KEY] == "0"
    assert sd.child_env({"PATH": "x"})[sd.ENV_KEY] == "0"
    assert sd.refusal_for_spawn({}) is None


def test_engine_hop_chain_is_refused_at_the_grandchild():
    top = sd.child_env({})  # launched from a shell: a top-level session, depth 0
    assert sd.session_depth(top) == 0
    child = sd.child_env(top)  # spawned by a test of that session: depth 1 runs
    assert sd.refusal_for_spawn(top) is None
    assert sd.session_depth(child) == 1
    assert sd.refusal_for_spawn(child) is not None  # its own spawn would be depth 2


def test_spawner_inside_a_test_counts_a_hop_even_without_the_stamp():
    env = {sd.CURRENT_TEST_KEY: "t.py::x (call)"}
    assert sd.child_env(env)[sd.ENV_KEY] == "1"
    assert sd.refusal_for_spawn(env) is None


def test_raw_hop_is_counted_by_session_depth_and_grandchild_exceeds_max():
    top_level_test_env = {sd.ENV_KEY: "0", sd.CURRENT_TEST_KEY: "t.py::x (call)"}
    assert sd.session_depth({}) == 0
    assert sd.session_depth(top_level_test_env) == 1  # raw child: depth 1, allowed
    stamped_child_test_env = {sd.ENV_KEY: "1", sd.CURRENT_TEST_KEY: "t.py::x (call)"}
    assert sd.session_depth(stamped_child_test_env) > sd.MAX_DEPTH  # raw grandchild: exits


# --- stdlib shadow on PYTHONPATH ------------------------------------------


def test_pythonpath_entry_with_stdlib_module_file_is_refused(tmp_path):
    (tmp_path / "bisect.py").write_text("")
    msg = sd.refusal_for_spawn({"PYTHONPATH": str(tmp_path)})
    assert msg and "bisect.py" in msg


def test_pythonpath_entry_with_stdlib_package_is_refused(tmp_path):
    (tmp_path / "random").mkdir()
    (tmp_path / "random" / "__init__.py").write_text("")
    msg = sd.refusal_for_spawn({"PYTHONPATH": os.pathsep.join(["", str(tmp_path / "missing"), str(tmp_path)])})
    assert msg and "random" in msg


def test_clean_pythonpath_and_dir_without_init_are_not_refused(tmp_path):
    (tmp_path / "mypkg").mkdir()
    (tmp_path / "mypkg" / "__init__.py").write_text("")
    (tmp_path / "random").mkdir()  # no __init__.py: a namespace dir, shadows nothing here
    (tmp_path / "notes.txt").write_text("")
    assert sd.refusal_for_spawn({"PYTHONPATH": str(tmp_path)}) is None
    assert sd.refusal_for_spawn({"PYTHONPATH": ""}) is None
    assert sd.refusal_for_spawn({}) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ships no stdlib readline")
def test_module_the_platform_stdlib_lacks_is_not_refused(tmp_path):
    (tmp_path / "readline.py").write_text("")  # pyreadline3's site-packages shim
    assert sd.refusal_for_spawn({"PYTHONPATH": str(tmp_path)}) is None


# --- self inclusion -------------------------------------------------------


def _env_for(path: str, test: str = "test_a") -> dict:
    return {sd.CURRENT_TEST_KEY: f"{path}::{test} (call)"}


def test_no_current_test_means_no_spawner_and_no_refusal(tmp_path):
    assert sd.spawner_file({}, str(tmp_path)) is None
    assert sd.selection_includes_spawner([str(tmp_path)], {}, str(tmp_path)) is None


def test_spawner_file_relative_resolves_against_rootdir(tmp_path):
    env = _env_for("pkg/tests/test_a.py")
    assert sd.spawner_file(env, str(tmp_path)) == _norm(tmp_path / "pkg" / "tests" / "test_a.py")


def test_spawner_file_strips_nodeid_class_and_param(tmp_path):
    env = {sd.CURRENT_TEST_KEY: "pkg/test_a.py::TestC::test_m[x::y] (setup)"}
    assert sd.spawner_file(env, str(tmp_path)) == _norm(tmp_path / "pkg" / "test_a.py")


def test_file_selection_refused_in_posix_shape(tmp_path):
    env = _env_for("pkg/tests/test_a.py")
    assert sd.selection_includes_spawner(["pkg/tests/test_a.py"], env, str(tmp_path))
    assert sd.selection_includes_spawner(["pkg/tests/test_a.py::test_a"], env, str(tmp_path))
    assert sd.selection_includes_spawner(["-q", "-p", "no:cacheprovider", "pkg/tests/test_a.py"], env, str(tmp_path))


def test_directory_selection_refused_for_any_containing_dir(tmp_path):
    env = _env_for("pkg/tests/test_a.py")
    for sel in ("pkg/tests", "pkg/tests/", "pkg", ".", str(tmp_path / "pkg")):
        assert sd.selection_includes_spawner([sel], env, str(tmp_path)), sel


def test_absolute_spawner_path_and_relative_selection(tmp_path):
    env = _env_for(str(tmp_path / "pkg" / "test_a.py"))
    assert sd.selection_includes_spawner(["pkg/test_a.py"], env, str(tmp_path))
    assert sd.selection_includes_spawner(["pkg"], env, str(tmp_path))


def test_unrelated_selection_is_not_refused(tmp_path):
    env = _env_for("pkg/tests/test_a.py")
    for sel in (
        ["pkg/tests/test_b.py"],
        ["pkg/tests_other"],
        ["pkg/tests/test_a.pyc"],
        ["other"],
        ["pkg/tests/test_a.py.bak"],
    ):
        assert sd.selection_includes_spawner(sel, env, str(tmp_path)) is None, sel


def test_option_values_are_not_mistaken_for_targets(tmp_path):
    env = _env_for("pkg/tests/test_a.py")
    sel = ["--rootdir", str(tmp_path), "-k", "pkg", "pkg/tests/test_b.py", "--basetemp=" + str(tmp_path)]
    assert sd.selection_includes_spawner(sel, env, str(tmp_path)) is None
    assert sd.selection_includes_spawner(sel + ["--", "pkg/tests/test_a.py"], env, str(tmp_path))


def test_empty_selection_counts_as_rootdir(tmp_path):
    env = _env_for("pkg/tests/test_a.py")
    assert sd.selection_includes_spawner([], env, str(tmp_path))
    assert sd.selection_includes_spawner(["-q"], env, str(tmp_path))


def test_selection_outside_rootdir_not_refused(tmp_path):
    root = tmp_path / "repo"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    env = _env_for("pkg/test_a.py")
    assert sd.selection_includes_spawner([str(other)], env, str(root)) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path shapes")
def test_windows_shapes_backslash_drive_and_case(tmp_path):
    env = {sd.CURRENT_TEST_KEY: f"{str(tmp_path).upper()}\\pkg\\test_a.py::t (call)"}
    assert sd.selection_includes_spawner(["pkg\\test_a.py"], env, str(tmp_path))
    assert sd.selection_includes_spawner([str(tmp_path / "pkg").lower()], env, str(tmp_path))
    assert sd.selection_includes_spawner(["pkg\\test_b.py"], env, str(tmp_path)) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX is case-sensitive")
def test_posix_case_sensitive(tmp_path):
    env = _env_for("pkg/test_a.py")
    assert sd.selection_includes_spawner(["pkg/TEST_A.py"], env, str(tmp_path)) is None


# --- root conftest --------------------------------------------------------


class _Config:
    """pytest_configure only reaches option.basetemp and the plugin manager after the depth check."""

    class option:
        basetemp = None

    class pluginmanager:
        registered: list = []

        @classmethod
        def register(cls, module, name):
            cls.registered.append(name)


def _root_conftest_module():
    # Prefer the copy pytest already imported; under the coordinator_core/pytest.ini rootdir the
    # root conftest is not loaded, so execute it once under a private name (its import-time
    # effects are idempotent for this checkout).
    for mod in list(sys.modules.values()):
        if getattr(mod, "__file__", None) and _norm(mod.__file__) == _norm(ROOT_CONFTEST) and hasattr(mod, "pytest_configure"):
            return mod
    spec = importlib.util.spec_from_file_location("_root_conftest_under_test", ROOT_CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    saved_env, saved_path = dict(os.environ), list(sys.path)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        sys.path[:] = saved_path
    return mod


def _isolate_counted_marker(monkeypatch) -> None:
    """Start with no counted marker; the monkeypatch record restores the key after the test."""
    monkeypatch.setenv(sd.COUNTED_KEY, "")
    monkeypatch.delenv(sd.COUNTED_KEY)


def test_conftest_exits_before_guard_registration_beyond_max_depth(monkeypatch):
    _isolate_counted_marker(monkeypatch)
    mod = _root_conftest_module()
    cfg = _Config()
    cfg.pluginmanager.registered = []
    monkeypatch.setenv(sd.ENV_KEY, "1")
    monkeypatch.setenv(sd.CURRENT_TEST_KEY, "t.py::x (call)")
    with pytest.raises(BaseException) as exc:
        mod.pytest_configure(cfg)
    assert type(exc.value).__name__ == "Exit"
    assert exc.value.returncode == 4
    assert cfg.pluginmanager.registered == []


def test_conftest_stamps_session_depth_when_within_bound(monkeypatch):
    _isolate_counted_marker(monkeypatch)
    mod = _root_conftest_module()
    cfg = _Config()
    cfg.pluginmanager.registered = []
    monkeypatch.setenv(sd.ENV_KEY, "0")
    monkeypatch.setenv(sd.CURRENT_TEST_KEY, "t.py::x (call)")
    mod.pytest_configure(cfg)
    assert os.environ[sd.ENV_KEY] == "1"
    assert cfg.pluginmanager.registered == ["drive_root_write_guard"]
