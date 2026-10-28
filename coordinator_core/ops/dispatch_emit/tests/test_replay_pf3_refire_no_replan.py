"""P-F3 replay (204cb5/a59293): a refire resumes only a reviewed plan; a stopped run's unreviewed plan is never adopted."""

from __future__ import annotations

import json

import yaml

from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.stage_receipts import write_receipt
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

PRIOR_RUN = "warp-2026-10-01-gate-20261002T010101-abc123"


def _prior_run(repo):  # noqa: F811
    d = repo / "state" / "warp" / PRIOR_RUN
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"sizing_rel": REL}), encoding="utf-8")
    return d


def _resume(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", plan=PLAN_REL)
    sizing = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    return resumable_plan(sizing, REL, repo, "s")


def test_stopped_run_plan_with_no_review_receipt_is_not_adopted(repo):  # noqa: F811
    _plan(repo)
    _prior_run(repo)
    assert _resume(repo) is None


def test_plan_with_a_prior_run_plan_review_receipt_resumes(repo):  # noqa: F811
    _plan(repo)
    write_receipt(_prior_run(repo), "plan_review", "ready", PLAN_REL)
    got = _resume(repo)
    assert got is not None and got.incomplete == ("A1",)


def test_receipt_naming_another_plan_does_not_vouch(repo):  # noqa: F811
    _plan(repo)
    write_receipt(_prior_run(repo), "plan_review", "ready", "docs/plans/other.md")
    assert _resume(repo) is None


def test_reviewed_status_counts_as_review_evidence(repo):  # noqa: F811
    _plan(repo)
    _prior_run(repo)
    path = repo / PLAN_REL
    path.write_text(path.read_text(encoding="utf-8").replace("run_id: fixture", "run_id: fixture\nstatus: reviewed"), encoding="utf-8")
    assert _resume(repo) is not None


def test_plan_staged_for_execute_counts_as_reviewed(repo):  # noqa: F811
    _plan(repo)
    (_prior_run(repo) / "stage-receipt.json").write_text("{}", encoding="utf-8")
    assert _resume(repo) is not None


def test_hand_authored_plan_with_no_prior_run_resumes(repo):  # noqa: F811
    _plan(repo)
    assert _resume(repo) is not None


def test_refire_under_a_new_run_id_carries_every_predecessor_note(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit.ask_gate import gate
    from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import _landed_repo, _put_s

    prev, new = "ask-20261021T233958-73b78a", "warp-2026-10-21-x-73b78a-20261021T235625-cfe57e"
    notes = [f"note {i}" for i in range(13)]
    _put_s(repo, requirement_register={"rows": [{"id": "R1", "status": "open"}]})
    _landed_repo(repo, (("A1", "coded"), ("A2", "coded")))
    prev_dir = rl.run_dir(repo, prev)
    prev_dir.mkdir(parents=True, exist_ok=True)
    (prev_dir / rl.NOTES_FILE).write_text(
        "".join(json.dumps({"note": n, "at": f"2026-10-21T23:{i:02d}:00Z"}) + "\n" for i, n in enumerate(notes)),
        encoding="utf-8",
    )
    rl.start(repo, new, resumed_from=prev)
    assert gate(repo, REL, run_id=new).resume_ledger["revise_notes"] == notes
