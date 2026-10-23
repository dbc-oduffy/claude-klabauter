"""since_halt_base precedence and the run_first_halts walk, over a real git repo under tmp_path."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import emit, halt_baseline
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD, halted_record_message

pytestmark = pytest.mark.spawns_process

PLAN = "docs/plans/p.md"
A, B, C = "a" * 40, "b" * 40, "c" * 40


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _halt(root: Path, run_id: str) -> str:
    msg = halted_record_message(run_id, {"halted": "ledger-check", HALT_PLAN_FIELD: PLAN})
    _git(root, "commit", "--allow-empty", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def test_run_first_halts_is_the_older_of_two_halts_of_one_run(tmp_path):
    root = _repo(tmp_path)
    h1 = _halt(root, "run-1")
    _git(root, "commit", "--allow-empty", "-q", "-m", "product")
    h2 = _halt(root, "run-1")
    state = emit.resume_landed_state(root, PLAN)
    assert state.run_first_halts == {"run-1": h1}
    assert state.run_halts["run-1"][1] == h2
    assert halt_baseline.first_halt_sha(state, "run-1") == h1


def test_precedence_table(tmp_path):
    state = emit.ResumeLanded(frozenset(), None, run_halts={"r": (frozenset(), B)}, run_first_halts={"r": A})
    base = lambda resume, head=None, st=state: halt_baseline.since_halt_base(tmp_path, resume, PLAN, head, st)
    assert base({"run_id": "r", "since_sha": C, "halt_sha": B}, "h") == (C, "param")
    assert base({"run_id": "r", "halt_sha": B}, "h") == (A, "resolved")
    assert base({"run_id": "zz", "halt_sha": B}, "h") == (B, "param")
    assert base({"run_id": "zz"}, "h") == ("h", "stage-head")
    assert base({"run_id": "zz", "since_sha": "short"}, None) == (None, None)


def test_param_present_spawns_no_git(tmp_path, monkeypatch):
    import coordinator_core.git.run as git_run

    calls: list = []
    monkeypatch.setattr(git_run, "run_git", lambda *a, **k: calls.append(a))

    def _no_walk(*a, **k):
        raise AssertionError("a since_sha param must not trigger resume_landed_state")

    monkeypatch.setattr(emit, "resume_landed_state", _no_walk)
    assert halt_baseline.since_halt_base(tmp_path, {"run_id": "r", "since_sha": A}, PLAN, None) == (A, "param")
    assert calls == []


def test_unresolved_state_reads_git_once(tmp_path):
    root = _repo(tmp_path)
    h1 = _halt(root, "run-1")
    _halt(root, "run-1")
    assert halt_baseline.since_halt_base(root, {"run_id": "run-1"}, PLAN, "head") == (h1, "resolved")
