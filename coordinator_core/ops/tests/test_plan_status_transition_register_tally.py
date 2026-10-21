"""stamp-implemented writes `review_stamp.register` for a register-claiming plan and refuses a non-zero tally."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import parse_frontmatter, validate_frontmatter
from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.ops.dispatch_emit import register_gate
from coordinator_core.ops.tests.test_plan_status_transition_goal_refusal import _init_repo, _run_git
from coordinator_core.ops.tests.test_plan_status_transition_records_exit_criterion import (
    _FLAGS,
    _seed,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REGISTER_SIZING = "state/sizings/2026-10-14-register-fixture.yaml"
_PLAN_SCHEMA = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "plan.schema.json"
_HEAD = """schema: sizing-object
intent: "x"
estimate:
  tshirt: S
  provisional: true
route: spec-dispatch
detents: []
fork: null
xl_exit: null
status: routed
premise:
  provenance: read
  evidence: e
"""


def _row(rid, status="open", **kw):
    row = {"id": rid, "source_text": f"t {rid}", "surface": "ui", "status": status,
           "claimed_by": ["docs/plans/p.md"], "wired": False}
    row.update(kw)
    return row


def _write_sizing(root: Path, rows) -> None:
    body = yaml.safe_dump(
        {"requirement_register": {"sources": [{"doc": "b.md", "ref": "r"}], "rows": rows}},
        sort_keys=False,
    )
    path = root / _REGISTER_SIZING
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEAD + body, encoding="utf-8")


def _claiming_plan(root: Path, rows, *, review_stamp: str = "") -> Path:
    plan = _seed(root)
    _write_sizing(root, rows)
    text = plan.read_text(encoding="utf-8")
    claims = f"register_claims:\n  sizing: {_REGISTER_SIZING}\n  rows: [R1]\n{review_stamp}"
    plan.write_text(text.replace("status: approved\n", "status: approved\n" + claims, 1), encoding="utf-8")
    _run_git(["add", "."], root)
    _run_git(["commit", "-q", "-m", "claims"], root)
    return plan


def _stamp(plan):
    return pst.main(["stamp-implemented", "--plan", str(plan), *_FLAGS])


def _fm(plan):
    return parse_frontmatter(plan.read_text(encoding="utf-8"))["frontmatter"]


def test_implemented_flip_writes_the_all_zero_block(tmp_path):
    _init_repo(tmp_path)
    plan = _claiming_plan(tmp_path, [_row("R1", "met", wired=True)])

    assert _stamp(plan) in (0, 2)

    fm = _fm(plan)
    assert fm["status"] == "implemented"
    assert fm["review_stamp"]["register"] == {
        "sizing": _REGISTER_SIZING, "unclaimed": 0, "open": 0, "partial": 0, "unwired": 0}
    assert not [e for e in validate_frontmatter(fm, _PLAN_SCHEMA) if "review_stamp" in json.dumps(e, default=str)]


def test_register_joins_an_existing_review_stamp_mapping(tmp_path):
    _init_repo(tmp_path)
    plan = _claiming_plan(tmp_path, [_row("R1", "met", wired=True)], review_stamp="review_stamp:\n  note: kept\n")

    assert _stamp(plan) in (0, 2)

    stamp = _fm(plan)["review_stamp"]
    assert stamp["note"] == "kept"
    assert stamp["register"]["open"] == 0


@pytest.mark.parametrize(
    "row, count",
    [(_row("R1"), "open=1"), (_row("R1", "partial"), "partial=1"), (_row("R1", "met"), "unwired=1")],
)
def test_non_zero_tally_refuses_and_leaves_the_plan_unchanged(tmp_path, capsys, row, count):
    _init_repo(tmp_path)
    plan = _claiming_plan(tmp_path, [row])
    before = plan.read_text(encoding="utf-8")

    assert _stamp(plan) == 1

    assert plan.read_text(encoding="utf-8") == before
    err = capsys.readouterr().err
    assert "review_stamp.register" in err and count in err and _REGISTER_SIZING in err


def test_plan_without_register_claims_is_unchanged_by_the_register_leg(tmp_path):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)

    assert _stamp(plan) in (0, 2)

    fm = _fm(plan)
    assert fm["status"] == "implemented"
    assert "review_stamp" not in fm


def test_judge_writeback_ahead_of_the_stamp_yields_the_all_zero_block(tmp_path):
    _init_repo(tmp_path)
    plan = _claiming_plan(tmp_path, [_row("R1")])
    judged = {"criterion": {"status": "met", "observation": "ok", "register_rows": [
        {"id": "R1", "claim": "this-plan", "status": "met", "wired": True, "surface": "ui",
         "observed_ref": "x.py::f"}]}}

    assert register_gate.register_writeback(tmp_path, "plan.md", judged, "abc1234") == {
        "register_sizing": _REGISTER_SIZING}
    assert _stamp(plan) in (0, 2)

    fm = _fm(plan)
    assert fm["status"] == "implemented"
    assert {k: v for k, v in fm["review_stamp"]["register"].items() if k != "sizing"} == {
        "unclaimed": 0, "open": 0, "partial": 0, "unwired": 0}
