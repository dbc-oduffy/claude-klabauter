"""A row the EM credited through `workflow.revise credit_chunks` counts landed at the refire gate: a
plan whose only open row is credited resumes at its ledger as an all-landed resume, and the composed run
reaches verify then commit. Fixtures are transcribed from the incident run, never opened."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_KIND_FIELD,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _handler, gate
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.test_all_landed_refire_e2e import _MET, _run_composed
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import _landed_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PRIOR = "ask-20261018T000000-bbbbbb"
_REGISTER = {
    "sources": [{"doc": REL, "ref": "ask"}],
    "rows": [{"id": "R1", "status": "open", "source_text": "t", "anchor": "relayed", "surface": "internal", "claimed_by": []}],
}


def _shape(repo, rows=(("C1", "open"),), landed=(), credited=("C1",)):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=_REGISTER)
    _landed_repo(repo, rows, landed=list(landed))
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  writes:\n    - some/{i}.py\n" for i, _ in rows
    )
    (repo / PLAN_REL).write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )
    record = repo / RECORD_DIR_ROOT / _PRIOR
    record.mkdir(parents=True)
    (record / "halted.json").write_text(
        json.dumps({"halted": "refusal", "reason": "no folded stage manifest", "sizing_rel": REL}), encoding="utf-8"
    )
    (record / "run.json").write_text(json.dumps({"sizing_rel": REL}) + "\n", encoding="utf-8")
    if credited:
        lines = "".join(
            json.dumps({"chunk": c, "evidence": "landed by hand", "at": "2026-10-18T00:00:00Z"}) + "\n"
            for c in credited
        )
        (record / "credit.jsonl").write_text(lines, encoding="utf-8")


def test_a_credited_only_open_row_gates_to_the_all_landed_ledger_resume(repo):  # noqa: F811
    _shape(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.resume_rows is None
    assert v.resume_ledger[RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND
    assert v.resume_ledger["blocking"] == ["R1"]
    out = _handler({"sizing_path": REL, "run_id": "ask-20261019T000000-cccccc"}, repo_root=repo)
    assert "manifest_handle" in out and "error" not in out["manifest"], out  # interim: inline beside the handle
    assert read_reply(repo, out["manifest_handle"])["rows"] is not None


def test_without_a_credit_the_gate_returns_the_row_resume_the_real_run_took(repo):  # noqa: F811
    _shape(repo, credited=())
    out = _handler({"sizing_path": REL, "run_id": "ask-20261019T000000-cccccc"}, repo_root=repo)
    assert out.get("resume_ledger") is None
    assert out["resume_plan"] == PLAN_REL and out["resume_rows"] == ["C1"]


def test_a_partial_credit_keeps_the_row_resume_over_the_uncredited_rows_only(repo):  # noqa: F811
    _shape(repo, rows=(("C1", "open"), ("C2", "open")))
    v = gate(repo, REL)
    assert v.resume_ledger is None
    assert list(v.resume_rows) == ["C2"]


def test_the_composed_run_reaches_verify_then_commit_with_no_executor_leg(repo):  # noqa: F811
    _shape(repo)
    out = _run_composed(repo, _MET)
    labels = [c["label"] for c in out["calls"]]
    res = out["result"]
    assert res["halted_by"] is None, res["halted_by"]
    assert "no folded stage manifest" not in json.dumps(res)
    assert labels.index("verify") < labels.index("commit")
    assert not {"plan", "prior-art", "exec", "ledger-remaining", "ledger-replan"} & set(labels), labels
