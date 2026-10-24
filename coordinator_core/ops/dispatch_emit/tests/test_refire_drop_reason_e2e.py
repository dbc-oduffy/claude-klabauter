"""A refire through the gate stages every unfinished row; an unstaged row carries the gate's reason into the notes, and the stage refuses one without."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import halted_record_message
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.resume_snapshot import SNAPSHOT_NAME
from coordinator_core.ops.dispatch_emit.run_notes import DROP_FIELD, entries
from coordinator_core.ops.dispatch_emit.tests import test_refire_foreign_checkpoint_row_drop_incident as inc
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ROWS = [
    *inc._ROWS,
    ("L1", "open", ["some/l1.py"], None),
    ("L2", "open", ["some/l2.py"], None),
]
RUN = inc.REFIRE_RUN


def _build(repo) -> str:
    """The d5aca3 repo plus L1 (landed by this plan's checkpoint) and L2 (landed by a hand commit);
    returns the plan text as committed, before the gate stamps L1 and L2 coded."""
    saved, inc._ROWS = inc._ROWS, _ROWS
    try:
        spine = inc._spine()
    finally:
        inc._ROWS = saved
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    (repo / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text(spine, encoding="utf-8", newline="\n")
    _put(repo, tshirt="S", route="spec-dispatch")
    for rel in ("some/c0.py", "some/x.py", "some/y.py", "some/z.py", "some/l1.py", "some/l2.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("v = 0\n", encoding="utf-8")
    inc._commit_all(repo, "plan")
    (repo / "some/x.py").write_text("v = 1\n", encoding="utf-8")
    (repo / "some/y.py").write_text("v = 1\n", encoding="utf-8")
    inc._commit_all(
        repo, "checkpoint(wave 1): 2 rows — A, B", "-m", f"Checkpoint-Plan: {inc.OTHER_PLAN}\nCheckpoint-Base: abc1234"
    )
    (repo / "some/x.py").write_text("v = 2\n", encoding="utf-8")
    inc._commit_all(repo, halted_record_message("warp-other-run", {"halted": "ledger-check", "plan": inc.OTHER_PLAN}))
    (repo / "some/l1.py").write_text("v = 1\n", encoding="utf-8")
    inc._commit_all(repo, "checkpoint(wave 2): 1 rows — L1", "-m", f"Checkpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234")
    (repo / "some/l2.py").write_text("v = 1\n", encoding="utf-8")
    inc._commit_all(repo, "hand edit of l2")
    return spine


def _restage(repo, spine: str, reply: dict) -> dict:
    """Undo the gate's `coded` stamps and its manifest, then stage the gate's resume rows through the op seam."""
    (repo / PLAN_REL).write_text(spine, encoding="utf-8", newline="\n")
    record = repo / "state" / "warp" / RUN
    for name in ("manifest.json", "stage-receipt.json"):
        (record / name).unlink()
    return ask_stage._handler({"run_id": RUN, "plan_path": PLAN_REL, "only_rows": list(reply["resume_rows"])}, repo)


def _snapshot(repo):
    return repo / "scratch" / "warp" / RUN / SNAPSHOT_NAME


def test_gate_refire_stages_foreign_checkpoint_rows_and_stamps_landed_rows(repo):  # noqa: F811
    spine = _build(repo)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo)
    assert reply.get("halt") is None, reply
    manifest = read_reply(repo, reply["manifest_handle"])
    assert "error" not in manifest
    assert {"FA", "F3", "F4"} <= {r["id"] for r in manifest["rows"]}
    assert not {"L1", "L2"} & {r["id"] for r in manifest["rows"]}
    assert "dropped_rows" not in manifest
    stamped = (repo / PLAN_REL).read_text(encoding="utf-8")
    assert stamped != spine and stamped.count("disposition: coded") == 3
    reasons = json.loads(_snapshot(repo).read_text(encoding="utf-8"))["reasons"]
    assert reasons["L1"].startswith("landed at ") and not reasons["L1"].startswith("landed outside")
    assert reasons["L2"].startswith("landed outside the executor at ")


def test_an_unfinished_row_the_gate_left_out_is_staged_as_a_dropped_row_with_its_reason(repo):  # noqa: F811
    spine = _build(repo)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo)
    reasons = json.loads(_snapshot(repo).read_text(encoding="utf-8"))["reasons"]
    out = _restage(repo, spine, reply)
    assert "error" not in out, out
    manifest = read_reply(repo, out["manifest_handle"])
    assert {"FA", "F3", "F4"} <= {r["id"] for r in manifest["rows"]}
    dropped = {d["id"]: d["reason"] for d in manifest["dropped_rows"]}
    assert dropped == {"L1": reasons["L1"], "L2": reasons["L2"]}
    notes = repo / "state" / "warp" / RUN / "notes.jsonl"
    lines = [json.loads(x) for x in notes.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert sorted((x[DROP_FIELD], x["reason"]) for x in lines if DROP_FIELD in x) == sorted(dropped.items())
    assert entries(notes, RUN) == []


def test_the_stage_refuses_the_same_unfinished_rows_once_the_snapshot_holds_no_reasons(repo):  # noqa: F811
    spine = _build(repo)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo)
    snap = _snapshot(repo)
    body = json.loads(snap.read_text(encoding="utf-8"))
    del body["reasons"]
    snap.write_text(json.dumps(body), encoding="utf-8")
    out = _restage(repo, spine, reply)
    assert out["error"].startswith("ROWS-DROPPED-WITHOUT-REASON"), out
    assert "L1" in out["error"] and "L2" in out["error"]
    assert not (repo / "state" / "warp" / RUN / "manifest.json").exists()
