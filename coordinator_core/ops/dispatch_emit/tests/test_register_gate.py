"""The register gate demotes a met criterion unless every claimed row passes; the write-back cascades verdicts into the sizing."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.register_gate import register_gate as _register_gate
from coordinator_core.ops.dispatch_emit.register_gate import register_writeback

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


def test_surface_only_mismatch_on_met_wired_row_is_reported_not_refused(repo):
    review = _review([_good(surface="api")])
    assert _register_gate(repo, _PLAN, review) is None
    crit = review["criterion"]
    assert crit["status"] == "met"
    assert "differs from register surface" in crit["observation"]


def test_repeated_judge_leaves_one_surface_mismatch_note(repo):
    review = _review([_good(surface="api")])
    assert _register_gate(repo, _PLAN, review) is None
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["observation"].count("differs from register surface") == 1


def test_surface_mismatch_with_empty_observed_ref_still_refuses(repo):
    review = _review([_good(surface="api", observed_ref="")])
    reason = _register_gate(repo, _PLAN, review)
    assert reason is not None and "observed_ref is empty" in reason
    assert review["criterion"]["status"] == "not_met"


@pytest.mark.parametrize(
    "rows,fragment",
    [
        ([], "not judged"),
        ([_good(status="partial")], "judged status 'partial', not met"),
        ([_good(claim="other-plan")], "not judged"),
        ([_good(wired=False)], "wired: false"),
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


def test_claimed_row_the_ledger_judged_is_not_demoted_as_not_judged(repo):
    review = _review([])
    assert _register_gate(repo, _PLAN, review, ["r1"]) is None
    assert review["criterion"]["status"] == "met"


def test_claimed_row_absent_from_judge_and_ledger_still_demotes(repo):
    review = _review([])
    reason = _register_gate(repo, _PLAN, review, ["other"])
    assert reason is not None and "r1" in reason and "not judged" in reason
    assert review["criterion"]["status"] == "not_met"


def _claimed_row(repo, tail):
    _set_register(
        repo,
        "    - {id: r1, source_text: t1, anchor: a1, surface: ui, claimed_by: [docs/plans/p.md], " + tail + "}\n",
    )


@pytest.mark.parametrize("rows", [None, []])
def test_disk_met_wired_attributed_claimed_row_is_not_owed_a_judge_verdict(repo, rows):
    _claimed_row(repo, "status: met, wired: true, met_by: [abc1234]")
    review = _review(rows)
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["status"] == "met"


@pytest.mark.parametrize(
    "tail",
    [
        "status: open, wired: false",
        "status: met, wired: false, met_by: [abc1234]",
        "status: met, wired: true, met_by: []",
        "status: met, wired: true",
    ],
)
def test_claimed_row_not_fully_met_on_disk_still_reports_not_judged(repo, tail):
    _claimed_row(repo, tail)
    review = _review(None)
    reason = _register_gate(repo, _PLAN, review)
    assert reason is not None and "r1" in reason and "not judged" in reason
    assert review["criterion"]["status"] == "not_met"


def test_judged_not_met_verdict_demotes_a_disk_met_row(repo):
    _claimed_row(repo, "status: met, wired: true, met_by: [abc1234]")
    reason = _register_gate(repo, _PLAN, _review([_good(status="partial")]))
    assert reason is not None and "judged status 'partial', not met" in reason


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


_CITE_TURN_2 = "intent_source: pm-verbatim\nintent: take a nap\n"


def test_uncovered_turn_demotes_and_names_it(repo):
    _turns_sizing(repo, extra=_CITE_TURN_2)
    review = _review([])
    reason = _register_gate(repo, _PLAN, review)
    assert reason is not None and "PM turn 2" in reason and "take a nap" in reason
    assert review["criterion"]["status"] == "not_met"


def test_sibling_slice_met_criterion_survives_uncovered_shared_turn(repo):
    _turns_sizing(repo, extra=_CITE_TURN_2 + "em_analysis:\n  ask_parent: ask-parent-x ask-x\n")
    review = _review([])
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["status"] == "met"


def test_uncited_turn_is_not_owed(repo):
    _turns_sizing(repo)
    review = _review([])
    assert _register_gate(repo, _PLAN, review) is None
    assert review["criterion"]["status"] == "met"


_RUN = "ask-20261016T101500-abc123"
_SUBMITTED = 1792146000.0  # 2026-10-16T10:20:00Z
_ROW_ASK = "    - {id: R1, source_text: slice ask text, anchor: ask, surface: ui, status: met, wired: true, claimed_by: []}\n"


def _replay_sizing(repo, ts, extra="", resolution=""):
    (repo / _SIZING).write_text(
        "status: routed\nintent_source: em-elaborated\nintent: elaborated slice ask\n"
        f"pm_verbatims:\n  - {{turn: 19, ts: '{ts}', text: 'is the tree dirty'}}\n{resolution}"
        "requirement_register:\n  rows:\n" + _ROW_ASK + extra,
        encoding="utf-8",
    )
    run = repo / "scratch" / "warp" / _RUN
    run.mkdir(parents=True, exist_ok=True)
    (run / "submission.json").write_text(f'{{"submitted_at": {_SUBMITTED}}}\n', encoding="utf-8")


def test_pre_start_uncited_turn_leaves_criterion_met(repo):
    _replay_sizing(repo, "2026-10-16T10:19:39Z")
    review = _review([])
    assert _register_gate(repo, _PLAN, review, run_id=_RUN) is None
    assert review["criterion"]["status"] == "met"


def test_post_start_turn_demotes_naming_it(repo):
    _replay_sizing(repo, "2026-10-16T10:20:39Z")
    review = _review([])
    reason = _register_gate(repo, _PLAN, review, run_id=_RUN)
    assert reason is not None and "PM turn 19" in reason
    assert review["criterion"]["status"] == "not_met"


def test_post_start_turn_in_unrelated_turns_is_cleared(repo):
    _replay_sizing(repo, "2026-10-16T10:20:39Z")
    (repo / _SIZING).write_text(
        (repo / _SIZING).read_text(encoding="utf-8") + "  unrelated_turns:\n    - {turn: 19, reason: chat}\n",
        encoding="utf-8",
    )
    review = _review([])
    assert _register_gate(repo, _PLAN, review, run_id=_RUN) is None
    assert review["criterion"]["status"] == "met"


def test_run_id_defaults_to_the_sizings_resolution(repo):
    _replay_sizing(repo, "2026-10-16T10:20:39Z", resolution=f"resolution:\n  sha: abcdef1\n  run_id: {_RUN}\n")
    reason = _register_gate(repo, _PLAN, _review([]))
    assert reason is not None and "PM turn 19" in reason


def test_no_run_start_owes_only_cited_turns(repo):
    _replay_sizing(repo, "2026-10-16T10:20:39Z")
    assert _register_gate(repo, _PLAN, _review([])) is None


def test_unrelated_turns_clears_the_uncovered_turn(repo):
    _turns_sizing(repo, extra=_CITE_TURN_2)
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


def _pinned_plan(repo, text="shape back"):
    from coordinator_core.ops import requirement_register as rr

    _own_plan(
        repo,
        f"register_claims:\n  sizing: {_SIZING}\n  rows: [z]\n  pins: {{z: \"{rr.row_pin(text)}\"}}\n",
    )


def test_pinned_row_with_matching_text_passes(repo):
    _set_register(repo, _ROW_T)
    _pinned_plan(repo)
    assert _register_gate(repo, _PLAN, _review([_good(id="z")])) is None


def test_pinned_row_whose_source_text_changed_demotes_naming_row(repo):
    _set_register(repo, _ROW_T)
    _pinned_plan(repo)
    _set_register(repo, _ROW_T.replace("shape back", "something else"))
    review = _review([_good(id="z")])
    reason = _register_gate(repo, _PLAN, review)
    assert reason is not None and "register row z" in reason and "source_text changed" in reason
    assert review["criterion"]["status"] == "not_met"
    assert review["criterion"]["reason"] == reason


def test_pinned_row_with_whitespace_only_edit_passes(repo):
    _set_register(repo, _ROW_T)
    _pinned_plan(repo)
    _set_register(repo, _ROW_T.replace("shape back", "shape   back"))
    assert _register_gate(repo, _PLAN, _review([_good(id="z")])) is None


def test_plan_without_pins_ignores_a_source_text_edit(repo):
    _set_register(repo, _ROW_T.replace("shape back", "edited"))
    _own_plan(repo, f"register_claims:\n  sizing: {_SIZING}\n  rows: [z]\n")
    assert _register_gate(repo, _PLAN, _review([_good(id="z")])) is None


_WB_HEAD = """schema: sizing-object
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
_WB_TAIL = "# trailer outside the register\n"
_WB_SOURCES = [{"doc": "brief.md", "ref": "r@abc"}]


def _wb_row(rid, claimed=True, **kw):
    row = {"id": rid, "source_text": f"t {rid}", "surface": "ui", "status": "open",
           "claimed_by": [_PLAN] if claimed else [], "wired": False}
    row.update(kw)
    return row


def _wb_sizing_text(rows):
    block = yaml.safe_dump({"requirement_register": {"sources": _WB_SOURCES, "rows": rows}}, sort_keys=False)
    return _WB_HEAD + block + _WB_TAIL


@pytest.fixture
def wb_repo(tmp_path):
    """A bare-bones git dir: ``locked_rmw`` locks under the git common dir, resolved in-process."""
    git_dir = tmp_path / ".git"
    (git_dir / "refs").mkdir(parents=True)
    (git_dir / "objects").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / _SIZING).write_text(
        _wb_sizing_text([_wb_row("r1"), _wb_row("r2"), _wb_row("r3", claimed=False)]), encoding="utf-8"
    )
    (tmp_path / _PLAN).write_text(f"---\nsizing_object: {_SIZING}\n---\nbody\n", encoding="utf-8")
    return tmp_path


def _wb_judged(rid, status="met", wired=True):
    return {"id": rid, "claim": "this-plan", "status": status, "wired": wired, "surface": "ui",
            "observed_ref": "x.py::f"}


def _wb_review(rows, status="met"):
    return {"integration_stem": "s", "slices": 1, "fixes": 0,
            "criterion": {"status": status, "observation": "ok", "register_rows": rows}}


def _wb_register(root):
    return yaml.safe_load((root / _SIZING).read_text(encoding="utf-8"))["requirement_register"]


def test_writeback_met_rows_get_status_wired_met_by_progress_and_rollup(wb_repo):
    out = register_writeback(wb_repo, _PLAN, _wb_review([_wb_judged("r1"), _wb_judged("r2")]), "abc1234")

    assert out == {"register_sizing": _SIZING}
    reg = _wb_register(wb_repo)
    r1, r2, r3 = reg["rows"]
    assert (r1["status"], r1["wired"], r1["met_by"]) == ("met", True, [f"{_PLAN}@abc1234"])
    assert r1["last_progress_at"] and r2["status"] == "met"
    assert r3["status"] == "open" and "met_by" not in r3
    assert reg["rollup"]["coverage"] == {"met": 2, "total": 3}
    assert reg["rollup"]["met"] == 2 and reg["rollup"]["unclaimed"] == 1
    assert reg["sources"] == _WB_SOURCES


def test_writeback_moves_row_claimed_only_through_register_claims(wb_repo):
    (wb_repo / _PLAN).write_text(
        f"---\nsizing_object: {_SIZING}\nregister_claims:\n  sizing: {_SIZING}\n  rows: [r3]\n---\nbody\n",
        encoding="utf-8",
    )

    out = register_writeback(wb_repo, _PLAN, _wb_review([_wb_judged("r3")]), "abc1234")

    assert out == {"register_sizing": _SIZING}
    r3 = _wb_register(wb_repo)["rows"][2]
    assert (r3["status"], r3["wired"], r3["met_by"]) == ("met", True, [f"{_PLAN}@abc1234"])


def test_writeback_not_met_run_appends_no_met_by_and_advances_progress(wb_repo):
    register_writeback(
        wb_repo, _PLAN, _wb_review([_wb_judged("r1", "partial", False)], status="not_met"), "abc1234"
    )

    r1 = _wb_register(wb_repo)["rows"][0]
    assert r1["status"] == "open" and "met_by" not in r1
    assert r1["last_progress_at"]


def test_writeback_leaves_bytes_outside_the_register_block_unchanged(wb_repo):
    register_writeback(wb_repo, _PLAN, _wb_review([_wb_judged("r1")]), "abc1234")

    text = (wb_repo / _SIZING).read_text(encoding="utf-8")
    assert text.startswith(_WB_HEAD) and text.endswith(_WB_TAIL)


def test_schema_invalid_writeback_is_refused_and_named_and_file_untouched(wb_repo):
    bad = _wb_sizing_text([_wb_row("r1", surface="nope")])
    (wb_repo / _SIZING).write_text(bad, encoding="utf-8")

    out = register_writeback(wb_repo, _PLAN, _wb_review([_wb_judged("r1")]), "abc1234")

    assert "register write-back refused" in out["register_writeback_error"]
    assert (wb_repo / _SIZING).read_text(encoding="utf-8") == bad


def test_no_judged_rows_or_no_register_writeback_is_a_noop(wb_repo):
    before = (wb_repo / _SIZING).read_text(encoding="utf-8")
    assert register_writeback(wb_repo, _PLAN, _wb_review([]), "abc1234") == {}
    assert (wb_repo / _SIZING).read_text(encoding="utf-8") == before
    (wb_repo / _SIZING).write_text(_WB_HEAD, encoding="utf-8")
    assert register_writeback(wb_repo, _PLAN, _wb_review([_wb_judged("r1")]), "abc1234") == {}
    assert (wb_repo / _SIZING).read_text(encoding="utf-8") == _WB_HEAD
