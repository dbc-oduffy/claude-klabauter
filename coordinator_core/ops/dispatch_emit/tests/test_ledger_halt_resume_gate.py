"""resumable_plan / ask_gate admit a ledger-halt resume; every other all-landed case still refuses."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import LedgerHalt, ledger_halt_path, write_ledger_halt
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ALREADY_RAN = f"{PLAN_REL} already ran: every row is landed; residue is a new sizing naming it"
REGISTER = {
    "rows": [
        {"id": "R1", "status": "open"},
        {"id": "R2", "status": "partial"},
        {"id": "R3", "status": "met"},
        {"id": "R4", "status": "waived"},
    ]
}


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


def _landed_repo(repo, rows=(("A1", "open"), ("A2", "open"))):
    """A git repo whose plan rows are all checkpoint-landed, none coded (unless `rows` says so)."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=list(rows))
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x.txt")
    ids = ", ".join(i for i, _ in rows)
    _git(
        repo, "commit", "-q", "-m", f"checkpoint(wave 1): {len(rows)} rows — {ids}",
        "-m", f"Checkpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234",
    )


def _pointer(repo, plan=PLAN_REL, sizing=REL, blocking=("R1",)):
    write_ledger_halt(ledger_halt_path(repo, PLAN_REL), LedgerHalt("run-1", plan, sizing, tuple(blocking)))


def _put_s(repo, **over):
    _put(repo, tshirt="S", route="spec-dispatch", plan=PLAN_REL, **over)


def test_matching_pointer_resumes_with_resume_ledger_and_no_halt(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo)
    _pointer(repo, blocking=("R1", "R2"))
    v = gate(repo, REL)
    assert v.halt is None
    assert v.resume_ledger == {
        "run_id": "run-1", "plan": PLAN_REL, "blocking": ["R1", "R2"], "rulable_ids": ["R1", "R2"],
    }
    assert v.resume_rows is None and v.resume_plan is None
    assert v.to_json()["resume_ledger"]["plan"] == PLAN_REL


def test_arm_s_resume_through_the_resume_sink(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo)
    _pointer(repo)
    v = gate(repo, REL)
    assert v.arm == "s" and v.resume_ledger is not None and v.halt is None


def _load(repo):
    import yaml

    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", ["no_pointer", "other_plan", "other_sizing", "malformed", "coded_row"])
def test_every_other_case_refuses_unchanged(repo, case):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    rows = (("A1", "coded"), ("A2", "open")) if case == "coded_row" else (("A1", "open"), ("A2", "open"))
    _landed_repo(repo, rows)
    if case == "other_plan":
        _pointer(repo, plan="docs/plans/other.md")
    elif case == "other_sizing":
        _pointer(repo, sizing="state/sizings/other.yaml")
    elif case == "malformed":
        p = ledger_halt_path(repo, PLAN_REL)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
    elif case == "coded_row":
        _pointer(repo)
    with pytest.raises(SizingFireRefused) as exc:
        resumable_plan(_load(repo), REL, repo, "s")
    assert exc.value.fields == [ALREADY_RAN]
    v = gate(repo, REL)
    assert v.arm is None and ALREADY_RAN in v.halt["reason"]


def test_gate_adds_no_spawn_beyond_the_one_git_log(repo, monkeypatch):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo)
    _pointer(repo)
    real = subprocess.Popen
    spawns: list = []

    def spy(*a, **k):
        spawns.append(a[0] if a else k.get("args"))
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    resumable_plan(_load(repo), REL, repo, "s")
    assert len(spawns) == 1 and "log" in spawns[0]
