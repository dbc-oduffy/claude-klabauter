"""coordinator_core/ops/tests/test_plan_signoff_widening.py — plan.signoff same-or-higher-class widening re-sign."""

from __future__ import annotations

import pytest

from coordinator_core.frontmatter.schema_validate import _grouping_signoff_error
from coordinator_core.ops import plan_signoff as mod
from coordinator_core.ops.tests.test_plan_signoff import (  # noqa: F401  (root is a fixture)
    CUT,
    _fm,
    _resolve,
    _rows,
    _text,
    _with_ruled_out,
    root,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

WIDE = {"rows": ["A2", "A1"], "disposition": "wont_do"}
REF = "docs/plans/p.md"


def _sign(root, source, cut_set=None, **kw):
    params = {"plan_path": REF, "target": {"grouping": "ruled_out"}, "source": source, "on": "2026-10-09"}
    if cut_set is not None:
        params["cut_set"] = cut_set
    if source == "pm":
        params["pm_quote"] = kw.pop("pm_quote", "pm says yes")
    elif source == "apm":
        params.update(apm_ruling="apm rules", ruling_ref=REF)
    else:
        params.update(approver="group-em-44", ruling="close it", ruling_ref=REF)
    params.update(kw)
    return mod._handler(params, root)


def _block(root):
    return _fm(root)["grouping_approvals"]["ruled_out"]


def _first_cut_resolved(root, source):
    _with_ruled_out(root)
    _sign(root, source, CUT)
    assert _resolve(root, ["A2"])["exit_code"] == 0


@pytest.mark.parametrize("prior,new", [("g-em", "g-em"), ("g-em", "uhura"), ("uhura", "g-em"), ("g-em", "apm"),
                                       ("apm", "apm"), ("pm", "pm")])
def test_widening_accepted_keeps_prior_in_history_and_resolve_accepts(root, prior, new):
    _first_cut_resolved(root, prior)
    prior_record = _block(root)["signoff"]
    _sign(root, new, WIDE)
    block = _block(root)
    assert block["signoff"]["source"] == new
    assert block["signoff"]["history"][-1] == prior_record
    assert _grouping_signoff_error("ruled_out", block) is None
    assert _resolve_widened_ok(root)


def _resolve_widened_ok(root):
    done = _resolve(root, ["A1"])
    return done["exit_code"] == 0 and _rows(root)["A1"]["disposition"] == "wont_do"


def test_widening_names_the_block_approver(root):
    _first_cut_resolved(root, "g-em")
    _sign(root, "g-em", WIDE)
    assert _block(root)["approver"] == "G-EM"


@pytest.mark.parametrize("prior,new", [("apm", "g-em"), ("apm", "uhura"), ("pm", "g-em"), ("pm", "apm")])
def test_lower_class_resign_refused_bytes_unchanged(root, prior, new):
    _first_cut_resolved(root, prior)
    before = _text(root)
    names = {"apm": "the APM", "pm": "the PM"}
    with pytest.raises(ValueError, match=f"already signed off by {names[prior]}"):
        _sign(root, new, WIDE)
    assert _text(root) == before


def test_resign_without_cut_set_or_adding_nothing_refused(root):
    _first_cut_resolved(root, "g-em")
    before = _text(root)
    with pytest.raises(ValueError, match="must widen"):
        _sign(root, "g-em")
    with pytest.raises(ValueError, match="must widen"):
        _sign(root, "g-em", CUT)
    assert _text(root) == before


def test_resign_over_unresolved_prior_cut_refused_naming_resolve(root):
    _with_ruled_out(root)
    _sign(root, "g-em", CUT)
    before = _text(root)
    with pytest.raises(ValueError, match="resolve"):
        _sign(root, "g-em", WIDE)
    assert _text(root) == before


def test_rank_refusal_wins_over_unresolved_cut(root):
    _with_ruled_out(root)
    _sign(root, "apm", CUT)
    with pytest.raises(ValueError, match="lower-class"):
        _sign(root, "g-em", WIDE)


def test_pm_countersign_over_unresolved_delegated_cut_still_written(root):
    _with_ruled_out(root)
    _sign(root, "g-em", CUT)
    _sign(root, "pm", WIDE)
    block = _block(root)
    assert block["signoff"]["source"] == "pm" and block["signoff"]["history"][-1]["source"] == "g-em"


def test_pm_widening_keeps_prior_pm_record_with_ruling_ref(root):
    _first_cut_resolved(root, "pm")
    prior_record = dict(_block(root)["signoff"], ruling_ref=REF)
    p = root / "docs/plans/p.md"
    p.write_text(_text(root).replace("source: pm\n", f"source: pm\n      ruling_ref: {REF}\n", 1), encoding="utf-8")
    assert _block(root)["signoff"] == prior_record
    _sign(root, "pm", WIDE, pm_quote="pm widens")
    block = _block(root)
    assert block["signoff"]["history"][-1] == prior_record
    assert _grouping_signoff_error("ruled_out", block) is None


def test_pm_non_widening_resign_refused(root):
    _first_cut_resolved(root, "pm")
    with pytest.raises(ValueError, match="already signed off by the PM; nothing to upgrade or overwrite"):
        _sign(root, "pm", CUT)
    with pytest.raises(ValueError, match="nothing to upgrade"):
        _sign(root, "pm")


def test_row_target_apm_over_apm_still_refused(root):
    params = {"plan_path": REF, "target": {"rows": ["A1"]}, "source": "apm", "apm_ruling": "ok",
              "ruling_ref": REF, "on": "2026-10-09"}
    mod._handler(params, root)
    with pytest.raises(ValueError, match="already signed off by the APM"):
        mod._handler(params, root)
