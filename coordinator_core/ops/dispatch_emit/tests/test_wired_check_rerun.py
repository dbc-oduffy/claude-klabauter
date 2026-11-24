"""rerun_wired_check runs only an admissible single-stage read; everything else demotes unspawned."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import wired_check
from coordinator_core.ops.dispatch_emit.wired_check import NOT_RERUNNABLE, rerun_wired_check


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("print('boom')\nraise SystemExit(3)\n", encoding="utf-8")
    (tmp_path / "slow.py").write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    (tmp_path / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    tracked = frozenset({"ok.py", "bad.py", "slow.py"})
    monkeypatch.setattr(
        wired_check.census, "tracked_targets",
        lambda root, cands: frozenset(c for c in cands if c in tracked),
    )
    return tmp_path


@pytest.fixture
def spawns(monkeypatch):
    calls = []
    real = subprocess.run

    def spy(argv, **kw):
        calls.append(argv)
        return real(argv, **kw)

    monkeypatch.setattr(wired_check.subprocess, "run", spy)
    return calls


def test_passing_python_script(repo, spawns):
    r = rerun_wired_check(repo, "python ok.py", timeout_s=20)
    assert r["passed"] is True and r["reason"] == ""
    assert len(spawns) == 1


def test_failing_script_reports_exit_and_output(repo):
    r = rerun_wired_check(repo, "python bad.py", timeout_s=20)
    assert r["passed"] is False
    assert r["reason"] == "exit 3"
    assert "boom" in r["output"]


def test_timeout_demotes(repo):
    r = rerun_wired_check(repo, "python slow.py", timeout_s=0.5)
    assert r["passed"] is False and r["reason"] == "timeout"


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest coordinator_core/roadmap/tests/test_wiring_clause.py",
        "coordinator-invoke sizing.accept_exit_criterion",
        "python untracked.py",
        "python -c 'print(1)'",
        "rm -rf x",
        "grep alpha data.txt | wc -l",
        "grep alpha data.txt && grep beta data.txt",
        "grep alpha data.txt ; grep beta data.txt",
        "grep alpha data.txt || true",
        "grep alpha data.txt > /dev/null",
        "grep alpha data.txt 2>/dev/null",
        "",
        "grep 'unterminated data.txt",
    ],
)
def test_inadmissible_never_spawns(repo, spawns, command):
    r = rerun_wired_check(repo, command)
    assert r == {"passed": False, "reason": NOT_RERUNNABLE, "output": ""}
    assert spawns == []


def test_unresolvable_head_demotes_without_error(repo, spawns, monkeypatch):
    monkeypatch.setattr(wired_check.shutil, "which", lambda name: None)
    r = rerun_wired_check(repo, "wc -l data.txt")
    assert r["passed"] is False and r["reason"] == NOT_RERUNNABLE
    assert spawns == []


def test_read_only_head_runs_via_argv_in_repo_root(repo, spawns):
    r = rerun_wired_check(repo, "grep -c alpha data.txt", timeout_s=20)
    if r["reason"] == NOT_RERUNNABLE:
        pytest.skip("grep not on PATH")
    assert r["passed"] is True and r["output"].strip() == "1"
    assert spawns and isinstance(spawns[0], list)
