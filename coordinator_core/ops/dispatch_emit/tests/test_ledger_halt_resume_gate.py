"""resumable_plan / ask_gate admit a ledger-halt resume; every other all-landed case still refuses."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    RESUME_LEDGER_KIND_FIELD,
    LedgerHalt,
    halted_record_message,
    ledger_halt_path,
    write_ledger_halt,
)
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ALREADY_RAN = (
    f"{PLAN_REL} already ran: every row is landed and nothing is left to judge; residue is a new sizing naming it"
)
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


def _landed_repo(repo, rows=(("A1", "open"), ("A2", "open")), landed=None):
    """A git repo whose plan rows are all checkpoint-landed (only `landed` ids when given), none coded
    (unless `rows` says so)."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=list(rows))
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x.txt")
    landed_ids = [i for i, _ in rows if landed is None or i in landed]
    ids = ", ".join(landed_ids)
    _git(
        repo, "commit", "-q", "-m", f"checkpoint(wave 1): {len(landed_ids)} rows — {ids}",
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
    assert {k: x for k, x in v.resume_ledger.items() if k not in ("em_discharged", "ledger_item_ids", "withheld_ids")} == {
        "run_id": "run-1", "plan": PLAN_REL, "blocking": ["R1", "R2"], "rulable_ids": ["R1", "R2"],
        "carried_met": [], "halt_blocking": ["R1", "R2"], "halt_sha": None, "remaining": {}, "revise_notes": [],
        "unlanded": [],
    }
    assert v.resume_rows is None and v.resume_plan is None
    assert v.to_json()["resume_ledger"]["plan"] == PLAN_REL


def test_arm_s_resume_through_the_resume_sink(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo)
    _pointer(repo)
    v = gate(repo, REL)
    assert v.arm == "s" and v.resume_ledger is not None and v.halt is None


def _halt_record(repo, staged=("A1", "A2"), plan=PLAN_REL, blocking=("R1",), run="run-1", owed=()):
    """A committed ledger-check halt: `state/warp/<run>` record plus a halt commit naming the plan."""
    d = repo / "state" / "warp" / run
    d.mkdir(parents=True, exist_ok=True)
    halted = {"halted": "ledger-check", "sizing_rel": REL}
    if plan is not None:
        halted["plan"] = plan
    if blocking is not None:
        halted["blocking"] = list(blocking)
    (d / "halted.json").write_text(json.dumps(halted), encoding="utf-8")
    manifest = {
        "rows": [{"id": i} for i in staged],
        "owed": [{"id": i} for i in owed],
        "gated": [],
    }
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _git(repo, "add", "state")
    _git(repo, "commit", "-q", "-m", halted_record_message(run, {"halted": "ledger-check", "plan": PLAN_REL}))


def test_unlanded_row_the_halted_run_staged_resumes_at_the_ledger_without_a_pointer(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo, landed=("A1",))
    _halt_record(repo)
    assert not ledger_halt_path(repo, PLAN_REL).exists()
    v = gate(repo, REL)
    assert v.halt is None
    assert v.resume_ledger["run_id"] == "run-1" and v.resume_ledger["blocking"] == ["R1", "R2"]
    assert v.resume_rows is None and v.resume_plan is None


def test_owed_row_the_halted_run_staged_counts_as_staged(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo, rows=(("A1", "open"), ("A2", "open")), landed=("A1",))
    _halt_record(repo, staged=("A1",), owed=("A2",))
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.incomplete == () and plan.ledger.run_id == "run-1"


def test_pointer_blocking_wins_when_it_names_the_halted_run(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo, landed=("A1",))
    _halt_record(repo, blocking=("R1",))
    _pointer(repo, blocking=("R2",))
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.ledger.blocking == ("R2",)


def test_record_without_plan_or_blocking_still_resumes(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo, landed=("A1",))
    _halt_record(repo, plan=None, blocking=None)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.incomplete == () and plan.ledger.blocking == ()


def test_row_the_halted_run_did_not_stage_keeps_resume_rows(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo, landed=("A1",))
    _halt_record(repo, staged=("A1",))
    v = gate(repo, REL)
    assert v.halt is None and v.resume_ledger is None
    assert v.resume_plan == PLAN_REL and v.resume_rows == ["A2"]


def test_pointer_alone_with_an_unlanded_row_is_not_a_ledger_resume(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo, landed=("A1",))
    _pointer(repo)
    v = gate(repo, REL)
    assert v.resume_ledger is None and v.resume_rows == ["A2"]


def test_coded_row_with_an_unlanded_row_and_a_halt_record_does_not_resume(repo):  # noqa: F811
    _put_s(repo, requirement_register=REGISTER)
    _landed_repo(repo, rows=(("A1", "coded"), ("A2", "open")), landed=("A1",))
    _halt_record(repo)
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.ledger is None and plan.incomplete == ("A2",)


def _load(repo):
    import yaml

    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", ["no_pointer", "other_plan", "other_sizing", "malformed", "coded_row"])
def test_every_other_case_resumes_as_all_landed_with_a_rulable_register(repo, case):  # noqa: F811
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
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.incomplete == () and plan.ledger.halted == ALL_LANDED_HALT_KIND
    assert plan.ledger.blocking == ("R1", "R2") and plan.ledger.halt_sha is None
    v = gate(repo, REL)
    assert v.halt is None and v.resume_ledger[RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND


@pytest.mark.parametrize("case", ["no_pointer", "coded_row"])
def test_every_other_case_refuses_when_the_register_has_nothing_to_judge(repo, case):  # noqa: F811
    _put_s(repo)
    rows = (("A1", "coded"), ("A2", "open")) if case == "coded_row" else (("A1", "open"), ("A2", "open"))
    _landed_repo(repo, rows)
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
