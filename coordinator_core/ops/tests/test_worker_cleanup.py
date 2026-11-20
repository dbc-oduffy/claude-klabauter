"""scratch.remove and proc.stop_own_child refuse out-of-scope targets; the stage clause and kill denial name them."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

import psutil

from coordinator_core.bash_guards import block_kill_by_port_or_tree as kill_guard
from coordinator_core.codex_runner import workflow
from coordinator_core.ops import worker_cleanup as wc


def test_scratch_remove_admits_only_codex_tmp_and_tmp(tmp_path):
    for rel in ("scratch/codex-tmp/a/b", "scratch/tmp/c", "src/keep", "scratch/other"):
        (tmp_path / rel).mkdir(parents=True)
    out = wc.remove_scratch(tmp_path, [
        "scratch/codex-tmp/a", str(tmp_path / "scratch/tmp/c"), "src/keep", "scratch/other",
        "scratch/codex-tmp", "scratch/codex-tmp/../other", "scratch/codex-tmp/gone",
    ])
    assert sorted(out["removed"]) == sorted(["scratch/codex-tmp/a", str(tmp_path / "scratch/tmp/c")])
    assert out["absent"] == ["scratch/codex-tmp/gone"]
    assert {r["path"] for r in out["refused"]} == {
        "src/keep", "scratch/other", "scratch/codex-tmp", "scratch/codex-tmp/../other"}
    assert (tmp_path / "src/keep").is_dir() and (tmp_path / "scratch/other").is_dir()
    assert not (tmp_path / "scratch/codex-tmp/a").exists()


def _child():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_stop_own_child_stops_a_descendant():
    child = _child()
    try:
        out = wc.stop_own_child(child.pid, os.getpid(), root_pid=os.getpid())
        assert out == {"stopped": child.pid}
        assert child.wait(5) is not None
    finally:
        child.kill()


def test_stop_own_child_refuses_non_descendant_and_ancestor():
    parent = psutil.Process().parent().pid
    child, sibling = _child(), _child()
    try:
        # Caller is `child` (its own root); `sibling` is a live process outside that root.
        assert "not a descendant" in wc.stop_own_child(sibling.pid, child.pid, root_pid=child.pid)["refused"]
        assert sibling.poll() is None
        # root_pid must be an ancestor of the caller; the child is not.
        assert "not an ancestor" in wc.stop_own_child(parent, os.getpid(), root_pid=child.pid)["refused"]
        assert "ancestors" in wc.stop_own_child(os.getpid(), os.getpid(), root_pid=os.getpid())["refused"]
        assert "codex ancestor" in wc.stop_own_child(child.pid, os.getpid())["refused"]
        assert child.poll() is None
    finally:
        child.kill()
        sibling.kill()


def test_stop_own_child_rejects_bad_pid():
    assert wc.stop_own_child(1, os.getpid())["refused"]
    assert wc.stop_own_child("9", os.getpid())["refused"]


def test_clause_invocations_are_not_dangerous_shapes():
    clause = workflow.CODEX_CLEANUP_CLAUSE
    assert "coordinator-invoke scratch.remove" in clause and "coordinator-invoke proc.stop_own_child" in clause
    invocations = re.findall(r"coordinator-invoke \S+ '[^']*'", clause)
    assert len(invocations) == 2
    for text in invocations:
        assert not re.search(r"remove-item|\brm\b|stop-process|taskkill|-recurse|-force", text, re.I)
        assert kill_guard._decide(text, os.getcwd()) is None


def test_kill_denial_names_the_rule_and_the_op():
    cmd = 'Get-CimInstance Win32_Process | ForEach-Object { Stop-Process -Id $_.ProcessId }'
    label, where = kill_guard._decide(cmd, os.getcwd())
    text = str(kill_guard._deny_record(label, where))
    assert "DENY-KILL-BY-PORT-OR-TREE" in text and "coordinator-invoke proc.stop_own_child" in text
