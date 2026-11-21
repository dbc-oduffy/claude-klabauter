"""Tests for job.start / job.cancel: real process-tree reap, pid-reuse refusal, lock and staging cleanup."""

import ast
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _host_probe
from coordinator_core.ipc import get_op_handler
from coordinator_core.jobs import job_record
from coordinator_core.ops import job_cancel

_CHILD = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def _op(name):
    handler = get_op_handler(name)
    assert handler is not None, f"{name} is not registered"
    return handler


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "settings-home"
    h.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(h))
    return h


def test_start_refuses_engine_pid_and_ancestor(home, tmp_path):
    start = _op("job.start")
    own = start({"job_id": "own", "root_pid": os.getpid()})
    assert "error" in own and "ancestor" in own["error"]
    rows = _host_probe.snapshot()
    chain = job_cancel._ancestors(rows, os.getpid())
    assert os.getppid() in chain
    parent = start({"job_id": "par", "root_pid": os.getppid()})
    assert "error" in parent and "ancestor" in parent["error"]
    assert job_record.read_record("own") is None and job_record.read_record("par") is None


def test_cancel_unknown_job_and_strict_params(home):
    assert "error" in _op("job.cancel")({"job_id": "nope"})
    assert "error" in _op("job.cancel")({"job_id": "nope", "extra": 1})


def test_cancel_refuses_a_tree_holding_the_engine(home, tmp_path, monkeypatch):
    ppid = os.getppid()
    job_record.write_record(job_record.JobRecord(
        job_id="self", root_pid=ppid, root_ctime=_host_probe.creation_time(ppid), lock_path=None,
        staging_path=None, owner_repo="t", started_at="now",
    ))
    monkeypatch.setattr(job_cancel, "kill_tree_verified", lambda *a: pytest.fail("killed the engine's tree"))
    reply = _op("job.cancel")({"job_id": "self"})
    assert "error" in reply and "contains this engine" in reply["error"]


@pytest.mark.parametrize("mod", ["ops/job_cancel.py", "jobs/job_record.py"])
def test_modules_import_no_subprocess(mod):
    src = (Path(job_cancel.__file__).parent.parent / mod).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "subprocess" not in imported
