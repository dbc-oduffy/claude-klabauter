import os
from pathlib import Path

import pytest

from coordinator_core.testing import fs_containment as fc

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _isolate_redirect_state(monkeypatch):
    import tempfile

    monkeypatch.setattr(fc, "_REDIRECT", None)
    monkeypatch.setattr(fc, "_INDEX_GUARD", {})
    monkeypatch.setattr(tempfile, "tempdir", None)
    for key in fc._TEMP_ENV:
        monkeypatch.setenv(key, os.environ.get(key, ""))


class _Opt:
    def __init__(self, basetemp=None):
        self.basetemp = basetemp


class _Cfg:
    def __init__(self, rootpath, basetemp=None, worker=False):
        self.rootpath = rootpath
        self.option = _Opt(basetemp)
        if worker:
            self.workerinput = {}


def _configure(monkeypatch, tmp_path, **kw):
    import tempfile

    temp = tmp_path / "Temp"
    temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(fc, "_SANCTIONED", [])
    monkeypatch.setattr(fc, "_WATCHED", [])
    repo = tmp_path / "myrepo"
    repo.mkdir(exist_ok=True)
    cfg = _Cfg(repo, **kw)
    fc.pytest_configure(cfg)
    return cfg, temp


pytest_plugins = ["pytester"]

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _dead_pid():
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    proc.wait()
    return proc.pid


def _seed_root(monkeypatch, tmp_path):
    import tempfile

    from coordinator_core import temp_layout

    temp = tmp_path / "Temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    root = temp_layout.pytest_basetemp_root(repo)
    root.mkdir(parents=True)
    return root


def test_configure_reaps_only_unmarked_dead_pid_run_dirs(tmp_path, monkeypatch):
    root = _seed_root(monkeypatch, tmp_path)
    dead = root / f"{_dead_pid()}-abcdef"
    import subprocess
    import sys

    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        live = root / f"{sleeper.pid}-abcdef"
        kept = root / f"{_dead_pid()}-fedcba"
        foreign = root / "not-a-run-dir"
        for d in (dead, live, kept, foreign):
            d.mkdir()
            (d / "f.txt").write_text("x")
        (kept / fc._KEPT_MARKER).touch()
        cfg, _ = _configure(monkeypatch, tmp_path)
        assert not dead.exists()
        assert live.is_dir() and kept.is_dir() and foreign.is_dir()
        assert Path(cfg.option.basetemp).parent == root
    finally:
        sleeper.kill()
        sleeper.wait()


def test_worker_configure_reaps_nothing(tmp_path, monkeypatch):
    root = _seed_root(monkeypatch, tmp_path)
    dead = root / f"{_dead_pid()}-abcdef"
    dead.mkdir()
    _configure(monkeypatch, tmp_path, worker=True)
    assert dead.is_dir()


_PEER = """
import os, sys, time
go, done, parent = sys.argv[1:4]
deadline = time.time() + 60
while not os.path.exists(go) and time.time() < deadline:
    time.sleep(0.05)
open(os.path.join(parent, "peer_status_before.txt"), "w").close()
os.mkdir(os.path.join(parent, ".peer-publish.stage-54060"))
os.mkdir(os.path.join(parent, ".peer-publish.old-54060"))
open(done, "w").close()
"""

_SPAWN_THEN_WAIT = """
import os, subprocess, sys, time

def test_spawns_while_a_peer_writes():
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    open(os.environ["PEER_GO"], "w").close()
    deadline = time.time() + 60
    while not os.path.exists(os.environ["PEER_DONE"]) and time.time() < deadline:
        time.sleep(0.05)
    assert os.path.exists(os.environ["PEER_DONE"])
"""

_OWN_WRITE = """
from pathlib import Path

def _let_peer_write():
    import os, time
    open(os.environ["PEER_GO"], "w").close()
    deadline = time.time() + 60
    while not os.path.exists(os.environ["PEER_DONE"]) and time.time() < deadline:
        time.sleep(0.05)

def test_writes_outside():
    _let_peer_write()
    (Path.cwd().parent / "own_stray.txt").write_text("x")
"""

_CHILD_WRITE = """
import subprocess, sys
from pathlib import Path

def _let_peer_write():
    import os, time
    open(os.environ["PEER_GO"], "w").close()
    deadline = time.time() + 60
    while not os.path.exists(os.environ["PEER_DONE"]) and time.time() < deadline:
        time.sleep(0.05)

def test_child_writes_outside():
    _let_peer_write()
    target = Path.cwd().parent / "child_stray.txt"
    subprocess.run([sys.executable, "-c", "import sys; open(sys.argv[1], 'w').close()", str(target)], check=True)
"""


def _peer_run(pytester, tmp_path, monkeypatch, body):
    import subprocess
    import sys

    parent = pytester.path.parent
    go, done = tmp_path / "go", tmp_path / "done"
    monkeypatch.setenv("PEER_GO", str(go))
    monkeypatch.setenv("PEER_DONE", str(done))
    peer = subprocess.Popen(
        [sys.executable, "-c", _PEER, str(go), str(done), str(parent)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        pytester.makepyfile(test_inner=body)
        pytester.makeini("[pytest]\n")
        pytester._monkeypatch.setenv("PYTHONPATH", str(Path(fc.__file__).resolve().parents[2]))
        return pytester.runpytest_subprocess(
            "-p", "coordinator_core.testing.fs_containment", "-p", "no:cacheprovider",
            "--basetemp", str(tmp_path / "bt"),
        )
    finally:
        peer.kill()
        peer.wait()


def test_a_spawning_test_is_not_charged_a_concurrent_peers_writes(pytester, tmp_path, monkeypatch):
    result = _peer_run(pytester, tmp_path, monkeypatch, _SPAWN_THEN_WAIT)
    result.assert_outcomes(passed=1)
    assert (pytester.path.parent / "peer_status_before.txt").exists()


@pytest.mark.parametrize("body, stray", [(_OWN_WRITE, "own_stray.txt"), (_CHILD_WRITE, "child_stray.txt")])
def test_a_test_or_its_child_writing_outside_still_fails(pytester, tmp_path, monkeypatch, body, stray):
    result = _peer_run(pytester, tmp_path, monkeypatch, body)
    out = result.stdout.str()
    assert "left new entries outside the repo and scratch" in out
    assert stray in out
    assert "peer_status_before" not in out
    result.assert_outcomes(passed=1, errors=1)
