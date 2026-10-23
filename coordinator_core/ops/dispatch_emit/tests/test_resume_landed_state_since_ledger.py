"""resume_landed_state records rows landed after the newest ledger halt, with no extra git spawn."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import halted_record_message
from coordinator_core.ops.dispatch_emit.emit import resume_landed_state
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_REL = "docs/plans/p.md"


def _git(repo, *a):
    return subprocess.run(
        ["git", *a], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _commit(repo, name, subject, *trailers):
    (repo / name).write_text("x\n", encoding="utf-8")
    _git(repo, "add", name)
    args = ["commit", "-q", "-m", subject]
    if trailers:
        args += ["-m", "\n".join(trailers)]
    _git(repo, *args)


def _init(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")


def _halt(repo):
    msg = halted_record_message("run-1", {"halted": "ledger-check", "plan": PLAN_REL})
    _commit(repo, "h.txt", msg + "\nLands-Chunks: C2")


def test_checkpoint_after_ledger_halt_is_since_ledger_and_earlier_is_not(repo):  # noqa: F811
    _init(repo)
    _commit(repo, "a.txt", "checkpoint(wave 0): 1 rows — C1", f"Checkpoint-Plan: {PLAN_REL}")
    _halt(repo)
    _commit(repo, "b.txt", "checkpoint(wave 1): 1 rows — C7", f"Checkpoint-Plan: {PLAN_REL}")
    state = resume_landed_state(repo, PLAN_REL)
    assert state.rows == {"C1", "C2", "C7"}
    assert state.landed_since_ledger == frozenset({"C7"})


def test_no_ledger_halt_leaves_since_ledger_empty(repo):  # noqa: F811
    _init(repo)
    _commit(repo, "a.txt", "checkpoint(wave 0): 1 rows — C1", f"Checkpoint-Plan: {PLAN_REL}")
    state = resume_landed_state(repo, PLAN_REL)
    assert state.rows == {"C1"}
    assert state.landed_since_ledger == frozenset()


def test_walk_spawns_one_git_log(repo, monkeypatch):  # noqa: F811
    _init(repo)
    _halt(repo)
    _commit(repo, "b.txt", "checkpoint(wave 1): 1 rows — C7", f"Checkpoint-Plan: {PLAN_REL}")
    from coordinator_core.git import run as run_mod

    calls = []
    real = run_mod.run_git

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(run_mod, "run_git", counting)
    resume_landed_state(repo, PLAN_REL)
    assert len(calls) == 1
