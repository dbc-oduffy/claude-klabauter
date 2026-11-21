"""nested_session_guard: guarded_child_env, in-process refusal, and synthetic real children.

Every real child is a synthetic pytest under tmp_path whose deepest hop runs a trivial leaf test,
so a broken guard bounds at one extra trivial session and cannot recurse.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.testing import nested_session_guard as nsg
from coordinator_core.testing import spawn_depth as sd

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SCRUB = (
    sd.ENV_KEY, sd.COUNTED_KEY, sd.CURRENT_TEST_KEY, "PYTEST_ADDOPTS", "PYTHONPATH",
    nsg.XDIST_WORKER_KEY, "PYTEST_PLUGINS", "PYTEST_XDIST_TESTRUNUID",
)


def _top_level_env() -> dict:
    """Env of a depth-0 session the children spawn from (the root conftest stamps ENV_KEY=0)."""
    env = dict(os.environ)
    for key in _SCRUB:
        env.pop(key, None)
    env[sd.ENV_KEY] = "0"
    return env


# --- guarded_child_env ------------------------------------------------------


# --- in-process refusal -----------------------------------------------------


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


def _synth_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "consumer_repo"
    repo.mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n")
    (repo / "leaf_test_target.py").write_text(_LEAF)
    (repo / "child_test_target.py").write_text(_CHILD)
    return repo


def _run_pytest(repo: Path, target: str, env: dict, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(repo / "pytest.ini"),
         "--rootdir", str(repo), *extra, str(repo / target)],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW,
    )


def _child_result(repo: Path, mode: str) -> tuple:
    env = nsg.guarded_child_env(_top_level_env())
    env["SYNTH_MODE"] = mode
    proc = _run_pytest(repo, "child_test_target.py", env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads((repo / "result.json").read_text()), proc


def test_guarded_child_runs_at_depth_one_from_a_repo_without_coordinator_core(tmp_path):
    repo = _synth_repo(tmp_path)
    result, proc = _child_result(repo, "plain")
    assert result["depth"] == "1"
    assert "1 passed" in proc.stdout


@pytest.mark.parametrize("mode", ["guarded", "raw"])
def test_grandchild_is_refused_and_collects_nothing(tmp_path, mode):
    repo = _synth_repo(tmp_path)
    result, _ = _child_result(repo, mode)
    grand = result["grandchild"]
    assert grand["code"] == nsg.REFUSAL_CODE
    assert "passed" not in grand["out"] and "collected" not in grand["out"]
    assert "refused" in grand["out"]


def test_xdist_worker_session_is_not_refused(tmp_path):
    repo = _synth_repo(tmp_path)
    env = nsg.guarded_child_env(_top_level_env())
    proc = _run_pytest(repo, "leaf_test_target.py", env, "-n", "2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout
    deep = dict(env, **{sd.ENV_KEY: "5", nsg.XDIST_WORKER_KEY: "gw0"})
    proc = _run_pytest(repo, "leaf_test_target.py", deep)
    assert proc.returncode == 0, proc.stdout + proc.stderr
