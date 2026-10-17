"""resumable_plan counts chunks a ledger-check halt commit landed, in the one git log it spends."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    LedgerHalt,
    halted_record_message,
    ledger_halt_path,
    write_ledger_halt,
)
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ROWS = [("C1", "open"), ("C2", "open")]
REGISTER = {"rows": [{"id": "R1", "status": "open"}]}


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


def _init(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=ROWS)
    _put(repo, tshirt="S", route="spec-dispatch", plan=PLAN_REL, requirement_register=REGISTER)


def _halt_commit(repo, chunks, halted_by="ledger-check", plan=PLAN_REL, n=[0]):
    n[0] += 1
    f = repo / f"h{n[0]}.txt"
    f.write_text("x\n", encoding="utf-8")
    _git(repo, "add", f.name)
    msg = halted_record_message("run-1", {"halted": halted_by, "plan": plan})
    msg += "\nLands-Chunks: " + ", ".join(chunks)
    _git(repo, "commit", "-q", "-m", msg)


def _checkpoint(repo, ids):
    f = repo / "ck.txt"
    f.write_text("x\n", encoding="utf-8")
    _git(repo, "add", f.name)
    _git(
        repo, "commit", "-q", "-m", f"checkpoint(wave 1): {len(ids)} rows — {', '.join(ids)}",
        "-m", f"Checkpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234",
    )


def _pointer(repo):
    write_ledger_halt(ledger_halt_path(repo, PLAN_REL), LedgerHalt("run-1", PLAN_REL, REL, ("R1",)))


def _load(repo):
    import yaml

    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))


def test_ledger_check_halt_commit_lands_rows_and_resumes_ledger(repo):  # noqa: F811
    _init(repo)
    _halt_commit(repo, ["C1", "C2"])
    _pointer(repo)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.incomplete == () and plan.ledger is not None


def test_gate_answers_resume_ledger(repo):  # noqa: F811
    _init(repo)
    _halt_commit(repo, ["C1", "C2"])
    _pointer(repo)
    v = gate(repo, REL)
    assert v.resume_ledger is not None and v.resume_rows is None and v.halt is None


def test_non_ledger_check_halt_leaves_rows_incomplete(repo):  # noqa: F811
    _init(repo)
    _halt_commit(repo, ["C1", "C2"], halted_by="test")
    _pointer(repo)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.incomplete == ("C1", "C2") and plan.ledger is None


def test_halt_plan_naming_another_plan_leaves_rows_incomplete(repo):  # noqa: F811
    _init(repo)
    _halt_commit(repo, ["C1", "C2"], plan="docs/plans/other.md")
    _pointer(repo)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.incomplete == ("C1", "C2") and plan.ledger is None


def test_checkpoint_plus_halt_commit_cover_every_row(repo):  # noqa: F811
    _init(repo)
    _checkpoint(repo, ["C1"])
    _halt_commit(repo, ["C2"])
    _pointer(repo)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.incomplete == () and plan.ledger is not None


def _run_halt(repo, run_id, halted_by="ledger-check", plan=PLAN_REL):
    f = repo / f"{run_id}.txt"
    f.write_text("x\n", encoding="utf-8")
    _git(repo, "add", f.name)
    _git(repo, "commit", "-q", "-m", halted_record_message(run_id, {"halted": halted_by, "plan": plan}))


def test_ledger_run_is_the_newest_ledger_check_halt(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    _init(repo)
    _run_halt(repo, "run-old")
    _run_halt(repo, "run-new")
    state = resume_landed_state(repo, PLAN_REL)
    assert state.ledger_run == "run-new"


def test_newer_refusal_halt_does_not_change_ledger_run(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    _init(repo)
    _run_halt(repo, "run-ledger")
    _run_halt(repo, "run-refusal", halted_by="refusal")
    assert resume_landed_state(repo, PLAN_REL).ledger_run == "run-ledger"


def test_ledger_halt_naming_another_plan_yields_no_ledger_run(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    _init(repo)
    _run_halt(repo, "run-x", plan="docs/plans/other.md")
    assert resume_landed_state(repo, PLAN_REL).ledger_run is None


def test_resume_landed_state_spends_one_git_process(repo, monkeypatch):  # noqa: F811
    from coordinator_core.git import run as run_mod
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    _init(repo)
    _run_halt(repo, "run-1")
    real = run_mod.run_git
    calls: list = []

    def spy(args, *a, **k):
        calls.append(args)
        return real(args, *a, **k)

    monkeypatch.setattr(run_mod, "run_git", spy)
    assert resume_landed_state(repo, PLAN_REL).ledger_run == "run-1"
    assert len(calls) == 1


def test_open_row_path_spends_one_git_process(repo, monkeypatch):  # noqa: F811
    from coordinator_core.git import run as run_mod

    _init(repo)
    _halt_commit(repo, ["C1"])
    real = run_mod.run_git
    calls: list = []

    def spy(args, *a, **k):
        calls.append(args)
        return real(args, *a, **k)

    monkeypatch.setattr(run_mod, "run_git", spy)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.incomplete == ("C2",)
    assert len(calls) == 1 and "log" in calls[0]


def _head(repo):
    """The full sha of HEAD."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def test_ledger_sha_is_the_halt_commit(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    _init(repo)
    _run_halt(repo, "run-old")
    _run_halt(repo, "run-new")
    expected = _head(repo)
    _run_halt(repo, "run-refusal", halted_by="refusal")
    assert resume_landed_state(repo, PLAN_REL).ledger_sha == expected


def test_ledger_sha_is_none_without_a_halt_commit(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    _init(repo)
    _checkpoint(repo, ["C1"])
    assert resume_landed_state(repo, PLAN_REL).ledger_sha is None


def test_resumable_plan_carries_halt_sha_on_record_and_pointer_paths(repo):  # noqa: F811
    _init(repo)
    _halt_commit(repo, ["C1"])
    _pointer(repo)
    sha = _head(repo)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.incomplete == ("C2",)
    _checkpoint(repo, ["C2"])
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.ledger is not None and plan.ledger.halt_sha == sha
