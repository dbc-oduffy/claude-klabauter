"""An all-landed plan with rulable register rows resumes at its ledger as an all-landed resume."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    RESUME_LEDGER_KIND_FIELD,
    write_run_outcome,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _handler, gate
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.sizing_fire import ALL_LANDED_NO_RUN, SizingFireRefused, resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import (
    ALREADY_RAN,
    _landed_repo,
    _load,
    _put_s,
    _pointer,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

CODED = (("A1", "coded"), ("A2", "coded"))
OPEN_REGISTER = {"rows": [{"id": "R1", "status": "open"}, {"id": "R2", "status": "met"}, {"id": "R3", "status": "partial"}]}
MET_REGISTER = {"rows": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "waived"}]}


def test_all_coded_rows_with_an_open_register_row_resume_all_landed(repo):  # noqa: F811
    _put_s(repo, requirement_register=OPEN_REGISTER)
    _landed_repo(repo, CODED)
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "s"
    assert v.resume_ledger[RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND
    assert v.resume_ledger["blocking"] == ["R1", "R3"] == v.resume_ledger["rulable_ids"]
    assert v.resume_ledger["run_id"] == ALL_LANDED_NO_RUN and v.resume_ledger["halt_sha"] is None
    assert v.resume_ledger["revise_notes"] == []
    assert v.resume_rows is None and v.resume_plan is None


def test_the_handler_stages_a_whole_zero_row_manifest(repo):  # noqa: F811
    _put_s(repo, requirement_register=OPEN_REGISTER)
    _landed_repo(repo, CODED)
    out = _handler({"sizing_path": REL, "run_id": "ask-20261018T000000-aaaaaa"}, repo_root=repo)
    assert "manifest_handle" in out and "manifest" not in out, out
    assert read_reply(repo, out["manifest_handle"])["rows"] == []
    assert out["resume_ledger"][RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND


def test_a_register_all_met_or_waived_keeps_the_already_ran_refusal(repo):  # noqa: F811
    _put_s(repo, requirement_register=MET_REGISTER)
    _landed_repo(repo, CODED)
    with pytest.raises(SizingFireRefused) as exc:
        resumable_plan(_load(repo), REL, repo, "s")
    assert exc.value.fields == [ALREADY_RAN]
    v = gate(repo, REL)
    assert v.arm is None and ALREADY_RAN in v.halt["reason"]


def test_a_not_met_recorded_outcome_resumes_even_with_a_settled_register(repo):  # noqa: F811
    _put_s(repo, requirement_register=MET_REGISTER)
    _landed_repo(repo, CODED)
    _pointer(repo, blocking=())
    write_run_outcome(
        repo, "run-1",
        {"inline_review": {}, "ledger_verdict": {"items": [{"id": "R1", "status": "not_met"}]}},
    )
    plan = resumable_plan(_load(repo), REL, repo, "s")
    assert plan.ledger.halted == ALL_LANDED_HALT_KIND and plan.ledger.run_id == "run-1"
    assert plan.ledger.blocking == ()


def test_a_met_recorded_outcome_keeps_the_refusal(repo):  # noqa: F811
    _put_s(repo, requirement_register=MET_REGISTER)
    _landed_repo(repo, CODED)
    _pointer(repo, blocking=())
    write_run_outcome(
        repo, "run-1",
        {"inline_review": {}, "ledger_verdict": {"items": [{"id": "R1", "status": "met"}]}},
    )
    with pytest.raises(SizingFireRefused):
        resumable_plan(_load(repo), REL, repo, "s")


def _example_game_repo_shape(repo, discharged):  # noqa: F811
    """Every plan row `open`: A1/A2 landed by one checkpoint commit, E1/E2 `performer: em` and
    discharged in the evidence sidecar for the ids in `discharged`. M sizing, rulable register rows."""
    _put(
        repo, tshirt="M", route="plan", plan=PLAN_REL,
        requirement_register={
            "sources": [{"doc": REL, "ref": "ask"}],
            "rows": [
                {"id": i, "status": "open", "source_text": "t", "anchor": "relayed", "surface": "internal", "claimed_by": []}
                for i in ("T2", "R2")
            ],
        },
    )
    _landed_repo(repo, (("A1", "open"), ("A2", "open"), ("E1", "open"), ("E2", "open")), landed=["A1", "A2"])
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  writes:\n    - some/{i}.py\n"
        + ("  performer: em\n" if i.startswith("E") else "")
        for i in ("A1", "A2", "E1", "E2")
    )
    (repo / PLAN_REL).write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )
    entries = "".join(f"  {i}:\n    recorded_at: '2026-10-17T00:00:00Z'\n    text: done by the EM\n" for i in discharged)
    sidecar = (repo / PLAN_REL).with_suffix(".evidence.yaml")
    sidecar.write_text(f"em_discharged:\n{entries}" if entries else "rows: {}\n", encoding="utf-8", newline="\n")


def test_em_discharged_rows_count_landed_and_the_gate_resumes_at_the_ledger(repo):  # noqa: F811
    _example_game_repo_shape(repo, ["E1", "E2"])
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "m_plus"
    assert v.resume_rows is None
    assert v.resume_ledger[RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND
    assert v.resume_ledger["blocking"] == ["T2", "R2"]
    out = _handler({"sizing_path": REL, "run_id": "ask-20261018T000000-bbbbbb"}, repo_root=repo)
    assert "manifest_handle" in out and "manifest" not in out, out
    assert read_reply(repo, out["manifest_handle"])["rows"] is not None


def test_an_undischarged_em_row_keeps_the_row_resume(repo):  # noqa: F811
    _example_game_repo_shape(repo, ["E1"])
    v = gate(repo, REL)
    assert v.arm == "m_plus" and v.resume_ledger is None
    assert list(v.resume_rows) == ["E2"]


def test_the_all_landed_resume_is_never_persisted(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit.ask_contract import ledger_halt_path

    _put_s(repo, requirement_register=OPEN_REGISTER)
    _landed_repo(repo, CODED)
    gate(repo, REL)
    assert not ledger_halt_path(repo, PLAN_REL).exists()
