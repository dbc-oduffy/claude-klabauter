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


def _own_plan(repo, claims=""):
    (repo / _PLAN).write_text(
        f"---\nplan_id: plan-x\nsizing_object: {_SIZING}\n{claims}---\nbody\n", encoding="utf-8"
    )


def _set_register(repo, rows):
    (repo / _SIZING).write_text("status: routed\nrequirement_register:\n  rows:\n" + rows, encoding="utf-8")


def test_unclaimed_open_row_demotes_even_when_plan_claims_nothing(repo):
    _set_register(repo, "    - {id: z, source_text: t, anchor: a, surface: ui, status: open, claimed_by: []}\n")
    review = _review([])
    reason = _register_gate(repo, _PLAN, review)
    assert reason is not None and "z" in reason
    assert review["criterion"]["status"] == "not_met"


def test_rows_claimed_only_via_register_claims_are_judged(repo):
    _set_register(repo, "    - {id: z, source_text: t, anchor: a, surface: ui, status: open, claimed_by: []}\n")
    _own_plan(repo, f"register_claims:\n  sizing: {_SIZING}\n  rows: [z]\n")
    review = _review([])
    reason = _register_gate(repo, _PLAN, review)
    assert reason is not None and "z" in reason and "not judged" in reason
    review = _review([_good(id="z")])
    assert _register_gate(repo, _PLAN, review) is None


def test_ruled_met_wired_and_other_plan_rows_do_not_block(repo):
    _set_register(
        repo,
        "    - {id: m, source_text: t, anchor: a, surface: ui, status: met, wired: true, claimed_by: []}\n"
        "    - {id: d, source_text: t, anchor: a, surface: ui, status: deferred, claimed_by: [],"
        " ruling: {source: pm, quote: later}}\n"
        "    - {id: o, source_text: t, anchor: a, surface: ui, status: open, claimed_by: [docs/plans/other.md]}\n",
    )
    review = _review([])
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["status"] == "met"


def test_unruled_deferred_row_still_blocks(repo):
    _set_register(repo, "    - {id: d, source_text: t, anchor: a, surface: ui, status: deferred, claimed_by: []}\n")
    assert _register_gate(repo, _PLAN, _review([])) is not None


def test_seed_plan_on_foreign_root_is_not_demoted_by_sibling_rows(repo):
    root = "state/sizings/2026-10-01-root.yaml"
    (repo / root).write_text(
        "status: routed\nrequirement_register:\n  rows:\n"
        "    - {id: mine, source_text: t, anchor: a, surface: ui, status: open, claimed_by: []}\n"
        "    - {id: sib, source_text: t, anchor: a, surface: ui, status: open, claimed_by: []}\n",
        encoding="utf-8",
    )
    _own_plan(repo, f"register_claims:\n  sizing: {root}\n  rows: [mine]\n")
    review = _review([_good(id="mine")])
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["status"] == "met"


def test_claims_sizing_equal_to_own_still_demotes_on_orphan(repo):
    _set_register(
        repo,
        "    - {id: mine, source_text: t, anchor: a, surface: ui, status: open, claimed_by: []}\n"
        "    - {id: z, source_text: t, anchor: a, surface: ui, status: open, claimed_by: []}\n",
    )
    _own_plan(repo, f"register_claims:\n  sizing: {_SIZING}\n  rows: [mine]\n")
    review = _review([_good(id="mine")])
    reason = _register_gate(repo, _PLAN, review)
    assert reason is not None and "z" in reason


def test_claims_sizing_outside_sizings_home_falls_back_to_own(repo):
    _set_register(repo, "    - {id: z, source_text: t, anchor: a, surface: ui, status: open, claimed_by: []}\n")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "x.yaml").write_text("status: routed\n", encoding="utf-8")
    _own_plan(repo, "register_claims:\n  sizing: docs/x.yaml\n  rows: [q]\n")
    reason = _register_gate(repo, _PLAN, _review([]))
    assert reason is not None and "z" in reason


def test_non_met_criterion_is_left_alone(repo):
    review = {"criterion": {"status": "not_met", "observation": "no"}}
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["observation"] == "no"


_ROW_T = "    - {id: z, source_text: shape back, anchor: a, surface: ui, status: met, wired: true, claimed_by: []}\n"


def _turns_sizing(repo, rows=_ROW_T, extra=""):
    (repo / _SIZING).write_text(
        "status: routed\npm_verbatims:\n  - {turn: 9, text: 'use the shape  back now'}\n"
        "  - {turn: 2, text: 'take a nap'}\n" + extra + "requirement_register:\n  rows:\n" + rows,
        encoding="utf-8",
    )


def test_uncovered_turn_demotes_and_names_it(repo):
    _turns_sizing(repo)
    review = _review([])
    reason = _register_gate(repo, _PLAN, review)
    assert reason is not None and "PM turn 2" in reason and "take a nap" in reason
    assert review["criterion"]["status"] == "not_met"


def test_unrelated_turns_clears_the_uncovered_turn(repo):
    _turns_sizing(repo)
    (repo / _SIZING).write_text(
        (repo / _SIZING).read_text(encoding="utf-8")
        + "  unrelated_turns:\n    - {turn: 2, reason: chat}\n",
        encoding="utf-8",
    )
    review = _review([])
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["status"] == "met"


def test_seed_plan_is_not_demoted_by_root_turns(repo):
    root = "state/sizings/2026-10-01-root.yaml"
    (repo / root).write_text(
        "status: routed\npm_verbatims:\n  - {turn: 2, text: 'take a nap'}\nrequirement_register:\n  rows:\n"
        "    - {id: mine, source_text: t, anchor: a, surface: ui, status: open, claimed_by: []}\n",
        encoding="utf-8",
    )
    _own_plan(repo, f"register_claims:\n  sizing: {root}\n  rows: [mine]\n")
    assert _register_gate(repo, _PLAN, _review([_good(id="mine")])) is None
