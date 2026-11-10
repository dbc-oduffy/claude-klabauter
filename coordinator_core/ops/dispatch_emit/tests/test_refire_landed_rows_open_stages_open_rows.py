"""A run's landing commit that touched every write of a row it left open must not credit that row: the refire stages it."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, ask_stage
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.resume_snapshot import SNAPSHOT_NAME
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests import test_refire_foreign_checkpoint_row_drop_incident as inc
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "warp-2026-10-24-d5aca3-20261024T170000-bbbbbb"
DELIVERABLE = "dlv-t-222222"


def _build(repo, lands: str | None) -> str:
    """A plan whose open rows LR (writes a, b) and OP (writes b) were both touched by one landing
    commit carrying ``Lands-Chunks: <lands>``; returns the plan text as committed."""
    saved, inc._ROWS = inc._ROWS, [
        ("LR", "open", ["some/a.py", "some/b.py"], None),
        ("OP", "open", ["some/b.py"], None),
    ]
    try:
        spine = inc._spine()
    finally:
        inc._ROWS = saved
    spine = spine.replace("run_id: fixture\n", f"run_id: fixture\ndeliverable_id: {DELIVERABLE}\n", 1)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    (repo / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text(spine, encoding="utf-8", newline="\n")
    _put(repo, tshirt="S", route="spec-dispatch")
    for rel in ("some/a.py", "some/b.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("v = 0\n", encoding="utf-8")
    inc._commit_all(repo, "plan")
    for rel in ("some/a.py", "some/b.py"):
        (repo / rel).write_text("v = 1\n", encoding="utf-8")
    trailer = "" if lands is None else f"\nLands-Chunks: {lands}"
    inc._commit_all(repo, "land the run", "-m", f"Deliverable-Id: {DELIVERABLE}{trailer}")
    return spine


def _rows(repo):
    return resumable_plan({"plan": PLAN_REL}, REL, repo)


def test_a_row_left_open_by_the_landing_commit_resumes_and_the_named_row_stays_landed(repo):  # noqa: F811
    _build(repo, "LR, docs")
    plan = _rows(repo)
    assert plan is not None and plan.ledger is None, plan
    assert plan.incomplete == ("OP",)
    assert dict(plan.not_run_reasons)["LR"].startswith("landed outside the executor at ")


def test_a_landing_without_a_lands_chunks_trailer_still_credits_every_row_it_touched(repo):  # noqa: F811
    _build(repo, None)
    plan = _rows(repo)
    assert plan is not None and plan.incomplete == (), plan


def test_the_default_refire_stages_exactly_the_open_row_with_no_dropped_reason_refusal(repo):  # noqa: F811
    spine = _build(repo, "LR, docs")
    reply = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo)
    assert reply.get("halt") is None, reply
    assert reply["resume_rows"] == ["OP"], reply
    manifest = read_reply(repo, reply["manifest_handle"])
    assert "error" not in manifest
    assert [r["id"] for r in manifest["rows"]] == ["OP"]
    snap = repo / "scratch" / "warp" / RUN / SNAPSHOT_NAME
    assert json.loads(snap.read_text(encoding="utf-8"))["reasons"]["LR"].startswith("landed outside")

    (repo / PLAN_REL).write_text(spine, encoding="utf-8", newline="\n")
    record = repo / "state" / "warp" / RUN
    for name in ("manifest.json", "stage-receipt.json"):
        (record / name).unlink()
    out = ask_stage._handler({"run_id": RUN, "plan_path": PLAN_REL, "only_rows": ["OP"]}, repo)
    assert "error" not in out, out
    staged = read_reply(repo, out["manifest_handle"])
    assert [r["id"] for r in staged["rows"]] == ["OP"]
    assert [d["id"] for d in staged["dropped_rows"]] == ["LR"]
