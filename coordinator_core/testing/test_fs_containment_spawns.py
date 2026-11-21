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
