"""A refire after a stage-refusal halt stages the plan's open rows; a zero-rows refusal names the row it dropped."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

HALTED = "ask-20261110T064432-4a1b28"
REFIRE = "warp-2026-11-10-refire-20261110T065916-7f8616"


def _stage(root, only_rows, run_id="r1", plan_rel=PLAN_REL):
    reply = _handler({"run_id": run_id, "plan_path": plan_rel, "only_rows": only_rows}, repo_root=root)
    return reply if "error" in reply else read_reply(root, reply["manifest_handle"])


def _row(rid: str, extra: str = "") -> str:
    return f"- id: {rid}\n  title: t{rid}\n  writes:\n    - some/{rid}.py\n{extra}"


def _write_plan(root, rows: str, status: str = "executing") -> None:
    (root / ".git").mkdir(exist_ok=True)
    plan = root / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\nrun_id: fixture\nstatus: {status}\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
        newline="\n",
    )


def test_refire_after_a_stage_refusal_halt_stages_the_open_row_through_the_gate(repo):  # noqa: F811
    (repo / ".git").mkdir()
    _plan(repo, rows=[("C1", "open")])
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL)
    rl.start(repo, HALTED)
    record = repo / RECORD_DIR_ROOT / HALTED
    write_halt_marker(record, halted="refusal", sizing_rel=REL, plan_rel=PLAN_REL)
    (record / "manifest.json").write_text(json.dumps({"rows": []}), encoding="utf-8")

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE}, repo)

    assert out.get("halt") is None and out["resume_rows"] == ["C1"], out
    assert [r["id"] for r in read_reply(repo, out["manifest_handle"])["rows"]] == ["C1"]


def test_an_empty_resume_set_with_no_snapshot_stages_the_open_row(tmp_path):
    _write_plan(tmp_path, _row("C1"))
    manifest = _stage(tmp_path, [])
    assert [r["id"] for r in manifest["rows"]] == ["C1"]


def test_an_empty_resume_set_still_leaves_closed_rows_unstaged(tmp_path):
    _write_plan(tmp_path, _row("C1", "  disposition: coded\n") + _row("C2"))
    manifest = _stage(tmp_path, [])
    assert [r["id"] for r in manifest["rows"]] == ["C2"]


def test_an_all_landed_plan_still_refuses_naming_the_closed_row(tmp_path):
    _write_plan(tmp_path, _row("C1", "  disposition: coded\n"))
    err = _stage(tmp_path, [])["error"]
    assert "zero dispatchable rows" in err and "C1" in err


def test_a_snapshot_that_dropped_the_row_still_refuses_and_names_it_with_the_reason(tmp_path):
    _write_plan(tmp_path, _row("C1"))
    resume_snapshot.write(tmp_path, "r1", PLAN_REL, ["C1"], [], reasons={"C1": "landed at abc1234"})
    err = _stage(tmp_path, ["C1"])["error"]
    assert "zero dispatchable rows" in err and "C1 (landed at abc1234)" in err


def test_a_named_resume_set_that_excludes_the_open_row_names_it_as_outside_only_rows(tmp_path):
    _write_plan(tmp_path, _row("C1") + _row("C2"))
    resume_snapshot.write(tmp_path, "r1", PLAN_REL, ["C1", "C2"], [])
    refused = _stage(tmp_path, ["C1"])["error"]
    assert "C1 (outside only_rows)" in refused and "C2 (outside only_rows)" in refused



def test_a_draft_plan_whose_snapshot_drops_the_row_without_a_reason_stages_it(tmp_path):
    _write_plan(tmp_path, _row("C1"), status="draft")
    resume_snapshot.write(tmp_path, "r1", PLAN_REL, ["C1"], [])
    manifest = _stage(tmp_path, ["C1"])
    assert [r["id"] for r in manifest["rows"]] == ["C1"]


def test_chain_kin_exempts_same_sizing_runs_and_still_names_a_different_sizing_peer(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_stage import _chain_kin

    monkeypatch.setattr(
        run_token,
        "held_records",
        lambda _root: [
            {"run_id": "me", "sizing_rel": "state/sizings/a.yaml"},
            {"run_id": "same", "sizing_rel": "state/sizings/a.yaml"},
            {"run_id": "peer", "sizing_rel": "state/sizings/b.yaml"},
        ],
    )
    assert _chain_kin(tmp_path, "me", ["same", "peer"]) == {"same"}
