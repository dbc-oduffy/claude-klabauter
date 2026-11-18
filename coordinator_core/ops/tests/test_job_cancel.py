"""Tests for job.start / job.cancel: real process-tree reap, pid-reuse refusal, lock and staging cleanup."""

import ast
import os
import subprocess
import sys
import time
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


@pytest.fixture
def tree():
    """A python child (root) that spawned a sleeping grandchild; yields (root Popen, grandchild pid)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD], stdout=subprocess.PIPE, text=True, creationflags=flags
    )
    grandchild = int(proc.stdout.readline())
    try:
        yield proc, grandchild
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


def _gone(pid, ctime):
    return not _host_probe.alive(pid, ctime)


def _job_files(tmp_path):
    lock = tmp_path / "job.lock"
    lock.write_text("held")
    staging = tmp_path / "staging"
    (staging / "sub").mkdir(parents=True)
    (staging / "sub" / "part.bin").write_text("x")
    return lock, staging


def _start(tmp_path, proc, job_id="job-1"):
    lock, staging = _job_files(tmp_path)
    reply = _op("job.start")(
        {
            "job_id": job_id, "root_pid": proc.pid, "lock_path": str(lock),
            "staging_path": str(staging), "owner_repo": "project-rag", "command": "index",
        },
        repo_root=tmp_path / "repo",
    )
    return reply, lock, staging


def test_start_writes_record_with_root_ctime(home, tmp_path, tree):
    proc, _ = tree
    reply, lock, staging = _start(tmp_path, proc)
    assert reply["ok"] is True
    rec = job_record.read_record("job-1")
    assert (rec.root_pid, rec.root_ctime) == (proc.pid, _host_probe.creation_time(proc.pid))
    assert (rec.lock_path, rec.staging_path) == (str(lock), str(staging))
    assert (home / "jobs" / "job-1.json").is_file()
    assert reply["root_ctime"] == rec.root_ctime


def test_cancel_reaps_tree_and_cleans(home, tmp_path, tree):
    proc, grandchild = tree
    gc_ctime = _host_probe.creation_time(grandchild)
    root_ctime = _host_probe.creation_time(proc.pid)
    _, lock, staging = _start(tmp_path, proc)
    reply = _op("job.cancel")({"job_id": "job-1"}, repo_root=tmp_path / "repo")
    assert reply["ok"] is True and reply["complete"] is True
    assert reply["lock_released"] and reply["staging_removed"] and not reply["already_gone"]
    assert {tuple(p) for p in reply["reaped"]} >= {(proc.pid, root_ctime), (grandchild, gc_ctime)}
    proc.wait(timeout=10)
    assert _gone(grandchild, gc_ctime) and _gone(proc.pid, root_ctime)
    assert not lock.exists() and not staging.exists()
    assert job_record.read_record("job-1") is None


def test_cancel_with_reused_pid_kills_nothing(home, tmp_path, tree):
    proc, grandchild = tree
    _, lock, staging = _start(tmp_path, proc)
    rec = job_record.read_record("job-1")
    job_record.write_record(job_record.JobRecord(**{**rec.__dict__, "root_ctime": rec.root_ctime + 12345}))
    reply = _op("job.cancel")({"job_id": "job-1"}, repo_root=tmp_path / "repo")
    assert reply["ok"] is True and reply["already_gone"] is True and reply["reaped"] == []
    assert proc.poll() is None
    assert _host_probe.creation_time(grandchild) is not None
    assert not lock.exists() and not staging.exists()


def test_incomplete_reap_leaves_lock_staging_and_record(home, tmp_path, tree, monkeypatch):
    proc, _ = tree
    _, lock, staging = _start(tmp_path, proc)
    monkeypatch.setattr(job_cancel, "kill_tree_verified", lambda tree_, snap, kill: ([], False, None))
    reply = _op("job.cancel")({"job_id": "job-1"}, repo_root=tmp_path / "repo")
    assert reply["ok"] is False and reply["complete"] is False and reply["reason"]
    assert lock.exists() and staging.exists()
    assert job_record.read_record("job-1") is not None


def test_cancel_refuses_unsafe_staging_but_still_reaps(home, tmp_path, tree):
    proc, _ = tree
    reply, lock, _staging = _start(tmp_path, proc)
    rec = job_record.read_record("job-1")
    job_record.write_record(job_record.JobRecord(**{**rec.__dict__, "staging_path": str(home)}))
    reply = _op("job.cancel")({"job_id": "job-1"}, repo_root=tmp_path / "repo")
    proc.wait(timeout=10)
    assert reply["complete"] is True and reply["staging_removed"] is False and reply["ok"] is False
    assert "settings home" in reply["reason"]
    assert home.is_dir()


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


def test_start_refuses_dead_root_unsafe_id_and_live_duplicate(home, tmp_path, tree):
    proc, _ = tree
    start = _op("job.start")
    assert "error" in start({"job_id": "../evil", "root_pid": proc.pid})
    assert "error" in start({"job_id": "dead", "root_pid": 2**22 + 7})
    assert "error" in start({"job_id": "rel", "root_pid": proc.pid, "lock_path": "relative.lock"})
    assert start({"job_id": "dup", "root_pid": proc.pid})["ok"] is True
    again = start({"job_id": "dup", "root_pid": proc.pid})
    assert "error" in again and "live record" in again["error"]


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
