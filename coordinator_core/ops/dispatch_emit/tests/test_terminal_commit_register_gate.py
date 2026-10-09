"""The terminal commit's register gate demotes a met criterion unless every claimed row passes."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.terminal_commit import _register_gate

_SIZING = "state/sizings/2026-10-09-x.yaml"
_PLAN = "docs/plans/p.md"

_REGISTER = """status: routed
requirement_register:
  rows:
    - {id: r1, source_text: t1, anchor: a1, surface: ui, status: open, claimed_by: [docs/plans/p.md], wired: false}
    - {id: r2, source_text: t2, anchor: a2, surface: api, status: open, claimed_by: [docs/plans/other.md], wired: false}
"""


def _good(**over):
    row = {"id": "r1", "claim": "this-plan", "status": "met", "wired": True, "surface": "ui",
           "observed_ref": "x.py::f"}
    row.update(over)
    return row


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / _SIZING).write_text(_REGISTER, encoding="utf-8")
    (tmp_path / _PLAN).write_text(
        f"---\nplan_id: plan-x\nsizing_object: {_SIZING}\n---\nbody\n", encoding="utf-8"
    )
    return tmp_path


def _review(rows):
    return {"criterion": {"status": "met", "observation": "ok", "register_rows": rows}}


def test_all_rows_met_and_wired_passes(repo):
    review = _review([_good()])
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["status"] == "met"


@pytest.mark.parametrize(
    "rows,fragment",
    [
        ([], "not judged"),
        ([_good(status="partial")], "judged status 'partial', not met"),
        ([_good(claim="other-plan")], "not judged"),
        ([_good(wired=False)], "wired: false"),
        ([_good(surface="api")], "differs from register surface"),
        ([_good(observed_ref="")], "observed_ref is empty"),
        ([_good(observed_ref=None)], "observed_ref is empty"),
    ],
)
def test_failing_row_demotes_naming_row(repo, rows, fragment):
    review = _review(rows)
    reason = _register_gate(repo, _PLAN, review)
    assert reason is not None and "r1" in reason and fragment in reason
    assert review["criterion"]["status"] == "not_met"
    assert review["criterion"]["reason"] == reason


def test_no_rows_claimed_by_plan_is_untouched(repo):
    other = "docs/plans/nobody.md"
    (repo / other).write_text(f"---\nsizing_object: {_SIZING}\n---\nbody\n", encoding="utf-8")
    review = _review([])
    assert _register_gate(repo, other, review) is None
    assert review["criterion"]["status"] == "met"


def test_no_register_is_untouched(repo):
    (repo / _SIZING).write_text("status: routed\n", encoding="utf-8")
    review = _review([])
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["status"] == "met"


def test_non_met_criterion_is_left_alone(repo):
    review = {"criterion": {"status": "not_met", "observation": "no"}}
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["observation"] == "no"
