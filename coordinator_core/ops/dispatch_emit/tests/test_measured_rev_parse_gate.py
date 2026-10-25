"""measured_rev refuses a declared .py path that fails ast.parse, spawn-free, writing no record."""

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args):
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "ok.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def test_joined_lines_refused_without_record_or_spawn(repo, monkeypatch):
    (repo / "bad.py").write_text("rung = 1 if rung not in RUNGS:\n    pass\n")
    _no_spawn(monkeypatch)
    reply = mr._handler({"run_id": "r1", "paths": ["bad.py"]}, repo)
    assert reply["refused"] == "measured-rev"
    assert reply["error"].startswith("measured-rev refused: py-unparseable:")
    assert "bad.py" in reply["error"] and "line 1" in reply["error"]
    assert not measured_rev_record_path(repo, "r1").exists()


def test_clean_nonpy_and_deleted_paths_pass(repo):
    (repo / "ok.py").write_text("x = 2\n")
    (repo / "notes.txt").write_text("not python (\n")
    reply = mr._handler({"run_id": "r2", "paths": ["ok.py", "notes.txt", "gone.py"]}, repo)
    assert "error" not in reply
    assert reply["sha"]
