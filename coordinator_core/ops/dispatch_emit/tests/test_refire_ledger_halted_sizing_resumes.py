"""A ledger-halted sizing re-fires into the ledger resume, even when the halt commit names no
`Halt-Plan:` and the pointer sits under the git common dir."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit.ask_contract import LedgerHalt, ledger_halt_path, write_ledger_halt
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REGISTER = {"rows": [{"id": "R1", "status": "open"}]}
RUN = "ask-run-1"


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


def _halted_repo(repo, subject_run=RUN, trailer=True):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=[("C1", "open")])
    _put(repo, tshirt="S", route="spec-dispatch", plan=PLAN_REL, requirement_register=REGISTER)
    (repo / "h.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "h.txt")
    msg = f"warp: record halted {subject_run} run artifacts\n\nHalted-By: ledger-check\n"
    if trailer:
        msg += "Lands-Chunks: C1\n"
    _git(repo, "commit", "-q", "-m", msg)


def _pointer(base, run_id=RUN):
    write_ledger_halt(ledger_halt_path(base, PLAN_REL), LedgerHalt(run_id, PLAN_REL, REL, ("R1",)))


def _load(repo):
    import yaml

    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))


def test_halt_commit_without_halt_plan_trailer_resumes_at_the_ledger(repo):  # noqa: F811
    _halted_repo(repo)
    _pointer(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.resume_ledger is not None
    assert v.resume_rows is None and v.resume_plan is None


def test_pointer_under_the_git_common_dir_resumes_at_the_ledger(repo):  # noqa: F811
    _halted_repo(repo)
    _pointer(resolve_git_common_dir(repo))
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan is not None and plan.ledger is not None and plan.incomplete == ()


def test_halt_commit_of_another_run_does_not_resume(repo):  # noqa: F811
    _halted_repo(repo, subject_run="other-run")
    _pointer(repo)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan is not None and plan.ledger is None and plan.incomplete == ("C1",)


def test_halt_commit_landing_no_chunk_does_not_resume(repo):  # noqa: F811
    _halted_repo(repo, trailer=False)
    _pointer(repo)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan is not None and plan.ledger is None and plan.incomplete == ("C1",)
