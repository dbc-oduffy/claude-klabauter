"""readiness_verdict turns a corrected citation, a missing register pin and an UNCHECKABLE anchor into
mechanical repairs, and still pulls a CONTRADICTED row with no corrected citation."""

from __future__ import annotations

from coordinator_core.ops import _anchor_premise
from coordinator_core.ops import plan_readiness_verdict as op

PASS_GATE = {"plan": "docs/plans/p.md", "verdict": "PREPPED", "withheld_rows": [], "classes": {}}


def _verdict(prep_gate=None, rows=None, anchor_findings=None, anchor_entries=None):
    return op.readiness_verdict(
        prep_gate or PASS_GATE, None, [], None, rows or [], None, None, None, None, "",
        anchor_findings, [], True, None, anchor_entries, None,
    )


def test_class_1_unresolved_with_corrected_path_is_a_citation_repair():
    row = {"verdict": "UNRESOLVED", "citation": "old/path.py", "evidence": "not found; corrected: `new/path.py`"}
    out = _verdict(rows=[row])
    assert (out["verdict"], out["pull_class"]) == ("pulled", "mechanical")
    (repair,) = out["mechanical_repairs"]
    assert repair["kind"] == "premise-citation-corrected"
    assert repair["instruction"] == "replace old/path.py with new/path.py"
    assert out["blocking"] == []


def test_unresolved_without_corrected_citation_still_pulls_judgment():
    out = _verdict(rows=[{"verdict": "UNRESOLVED", "citation": "old/path.py", "evidence": "gone"}])
    assert out["verdict"] == "judgment" and out["mechanical_repairs"] == []


def test_contradicted_without_corrected_citation_still_pulls_judgment():
    out = _verdict(rows=[{"verdict": "CONTRADICTED", "citation": "a.py:3", "evidence": "says otherwise"}])
    assert out["verdict"] == "judgment" and out["mechanical_repairs"] == []


def test_register_pin_missing_is_mechanical_and_names_the_applier():
    gate = {
        "plan": "docs/plans/p.md", "verdict": "NOT-PREPPED", "withheld_rows": [],
        "classes": {"REGISTER": {"status": "FAIL", "kind": "register-pin-missing", "detail": "omit R2"}},
    }
    out = _verdict(prep_gate=gate)
    assert (out["verdict"], out["pull_class"]) == ("pulled", "mechanical")
    (repair,) = out["mechanical_repairs"]
    assert repair["kind"] == "register-pin-missing"
    assert "plan.apply_mechanical_repairs" in repair["instruction"]


def test_register_rows_unclaimed_names_the_applier_too():
    gate = {
        "plan": "docs/plans/p.md", "verdict": "NOT-PREPPED", "withheld_rows": [],
        "classes": {"REGISTER": {"status": "FAIL", "kind": "register-rows-unclaimed", "detail": "omit R2"}},
    }
    (repair,) = _verdict(prep_gate=gate)["mechanical_repairs"]
    assert "plan.apply_mechanical_repairs" in repair["instruction"]


def test_other_register_defect_still_pulls_substantive():
    gate = {
        "plan": "docs/plans/p.md", "verdict": "NOT-PREPPED", "withheld_rows": [],
        "classes": {"REGISTER": {"status": "FAIL", "kind": "register-row-unknown", "detail": "R9"}},
    }
    out = _verdict(prep_gate=gate)
    assert (out["verdict"], out["pull_class"]) == ("pulled", "substantive")


def test_uncheckable_anchor_becomes_a_falsifier_repair_with_the_full_claim():
    claim = "x" * 200
    finding = f"anchor-premise-UNCHECKABLE: AP1 -- {claim[:160]}"
    out = _verdict(anchor_findings=[finding], anchor_entries=[{"id": "AP1", "claim": claim}])
    assert (out["verdict"], out["pull_class"]) == ("pulled", "mechanical")
    (repair,) = out["mechanical_repairs"]
    assert (repair["kind"], repair["target"]) == ("uncheckable-to-falsifier-row", "AP1")
    assert claim in repair["instruction"]
    assert out["blocking"] == []


def test_contradicted_anchor_is_not_converted():
    out = _verdict(anchor_findings=["anchor-premise-CONTRADICTED: AP1 -- c"])
    assert out["verdict"] == "judgment" and out["mechanical_repairs"] == []


def test_split_convertible_keeps_other_findings():
    kept, converted = _anchor_premise.split_convertible(
        ["anchor-premise-UNCHECKABLE: AP1 -- c", "anchor-premise-no-verdict: AP2"]
    )
    assert kept == ["anchor-premise-no-verdict: AP2"] and converted == [("AP1", "c")]
