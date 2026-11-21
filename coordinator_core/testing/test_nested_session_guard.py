"""nested_session_guard: guarded_child_env, in-process refusal, and synthetic real children.

Every real child is a synthetic pytest under tmp_path whose deepest hop runs a trivial leaf test,
so a broken guard bounds at one extra trivial session and cannot recurse.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.testing import nested_session_guard as nsg
from coordinator_core.testing import spawn_depth as sd

pytestmark = pytest.mark.complete

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SCRUB = (
    sd.ENV_KEY, sd.COUNTED_KEY, sd.CURRENT_TEST_KEY, "PYTEST_ADDOPTS", "PYTHONPATH",
    nsg.XDIST_WORKER_KEY, "PYTEST_PLUGINS", "PYTEST_XDIST_TESTRUNUID",
)


# --- guarded_child_env ------------------------------------------------------


def test_guarded_child_env_stamps_depth_plugin_and_engine_root():
    out = nsg.guarded_child_env({sd.CURRENT_TEST_KEY: "t.py::x (call)", "KEEP": "1"})
    assert out[sd.ENV_KEY] == "1"
    assert sd.CURRENT_TEST_KEY not in out
    assert out["KEEP"] == "1"
    assert f"-p {nsg.PLUGIN_MODULE}" in out["PYTEST_ADDOPTS"]
    assert nsg.ENGINE_ROOT in out["PYTHONPATH"].split(os.pathsep)


def test_guarded_child_env_from_a_shell_launcher_is_a_top_level_depth_zero():
    assert nsg.guarded_child_env({})[sd.ENV_KEY] == "0"


def test_guarded_child_env_caps_blas_pools_without_overriding():
    assert all(nsg.guarded_child_env({})[k] == "1" for k in nsg.BLAS_THREAD_KEYS)
    assert nsg.guarded_child_env({"OPENBLAS_NUM_THREADS": "4"})["OPENBLAS_NUM_THREADS"] == "4"


def test_guarded_child_env_appends_once_and_preserves_existing_values():
    base = {"PYTEST_ADDOPTS": "-q", "PYTHONPATH": "/x"}
    once = nsg.guarded_child_env(base)
    twice = nsg.guarded_child_env(once)
    assert once["PYTEST_ADDOPTS"] == f"-q -p {nsg.PLUGIN_MODULE}"
    assert twice["PYTEST_ADDOPTS"] == once["PYTEST_ADDOPTS"]
    assert twice["PYTHONPATH"].split(os.pathsep).count(nsg.ENGINE_ROOT) == 1
    assert twice["PYTHONPATH"].split(os.pathsep)[0] == "/x"
    assert base == {"PYTEST_ADDOPTS": "-q", "PYTHONPATH": "/x"}


# --- in-process refusal -----------------------------------------------------


def _clean_env(monkeypatch) -> None:
    """Remove the guard's keys, recording each so production writes are undone at teardown."""
    for key in _SCRUB:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _configure_with(monkeypatch, **env):
    _clean_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    nsg.pytest_configure(object())


def test_depth_zero_and_one_pass_and_are_stamped(monkeypatch):
    _configure_with(monkeypatch)
    assert os.environ[sd.ENV_KEY] == "0"
    _configure_with(monkeypatch, **{sd.ENV_KEY: "1"})
    assert os.environ[sd.ENV_KEY] == "1"
    _configure_with(monkeypatch, **{sd.ENV_KEY: "0", sd.CURRENT_TEST_KEY: "t.py::x (call)"})
    assert os.environ[sd.ENV_KEY] == "1"  # raw hop from a top-level test: child is depth 1


@pytest.mark.parametrize(
    "env",
    [
        {sd.ENV_KEY: "2"},
        {sd.ENV_KEY: "1", sd.CURRENT_TEST_KEY: "t.py::x (call)"},  # raw hop from a depth-1 test
    ],
)
def test_past_max_depth_exits_with_refusal_code(monkeypatch, env):
    with pytest.raises(BaseException) as exc:
        _configure_with(monkeypatch, **env)
    assert type(exc.value).__name__ == "Exit"
    assert exc.value.returncode == nsg.REFUSAL_CODE
    assert sd.ENV_KEY in str(exc.value)


def test_xdist_worker_is_never_refused_and_env_untouched(monkeypatch):
    _configure_with(monkeypatch, **{sd.ENV_KEY: "5", nsg.XDIST_WORKER_KEY: "gw0"})
    assert os.environ[sd.ENV_KEY] == "5"


def test_conftest_then_plugin_count_a_raw_hop_once(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv(sd.ENV_KEY, "0")
    monkeypatch.setenv(sd.CURRENT_TEST_KEY, "t.py::x (call)")
    # root conftest path: session_depth on os.environ, then the stamp write-back
    first = sd.session_depth(os.environ)
    os.environ[sd.ENV_KEY] = str(first)
    nsg.pytest_configure(object())  # plugin path, same process
    assert first == 1
    assert os.environ[sd.ENV_KEY] == "1"
    nsg.pytest_configure(object())
    assert os.environ[sd.ENV_KEY] == "1"


def test_counted_marker_from_another_pid_is_not_trusted(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv(sd.ENV_KEY, "1")
    monkeypatch.setenv(sd.CURRENT_TEST_KEY, "t.py::x (call)")
    monkeypatch.setenv(sd.COUNTED_KEY, f"{os.getpid() + 1}:1:1")  # inherited from a parent
    assert sd.session_depth(os.environ) == 2
    monkeypatch.setenv(sd.COUNTED_KEY, f"{os.getpid()}:1:1")  # this process already counted
    assert sd.session_depth(os.environ) == 1


# --- real synthetic children -----------------------------------------------

_LEAF = "def test_leaf():\n    pass\n"

_CHILD = '''
import json, os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).parent
ARGS = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ROOT / "pytest.ini"),
        "--rootdir", str(ROOT), str(ROOT / "leaf_test_target.py")]

def _run(env):
    r = subprocess.run(ARGS, cwd=str(ROOT), env=env, capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"code": r.returncode, "out": r.stdout + r.stderr}

def test_child():
    from coordinator_core.testing import nested_session_guard as nsg
    mode = os.environ["SYNTH_MODE"]
    result = {"depth": os.environ.get("COORDINATOR_PYTEST_DEPTH")}
    if mode == "guarded":
        result["grandchild"] = _run(nsg.guarded_child_env(os.environ))
    elif mode == "raw":
        result["grandchild"] = _run(dict(os.environ))
    (ROOT / "result.json").write_text(json.dumps(result))
'''
