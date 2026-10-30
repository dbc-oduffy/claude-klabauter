"""Tests for the premise-repair interface module."""

from coordinator_core.ops import _premise_repair as pr


def test_repair_kind_constants():
    assert pr.REPAIR_CITATION == "premise-citation-corrected"
    assert pr.REPAIR_FALSIFIER == "uncheckable-to-falsifier-row"
    assert pr.REPAIR_REGISTER_PIN == "register-pin-missing"


def test_corrected_citation_parses_plain_and_backticked():
    assert pr.corrected_citation({"evidence": "wrong path; corrected: src/a/b.py"}) == "src/a/b.py"
    assert pr.corrected_citation({"evidence": "corrected: `src/a b.py`. done"}) == "src/a b.py"
    assert pr.corrected_citation({"evidence": "see x. corrected: src/a.py:12."}) == "src/a.py:12"


def test_corrected_citation_absent_is_none():
    assert pr.corrected_citation({"evidence": "no marker here"}) is None
    assert pr.corrected_citation({"evidence": "corrected:"}) is None
    assert pr.corrected_citation({}) is None
    assert pr.corrected_citation({"evidence": 5}) is None
    assert pr.corrected_citation("corrected: x") is None


def test_convertible_both_sides():
    assert pr.convertible({"verdict": "UNCHECKABLE", "evidence": ""})
    assert pr.convertible(
        {"verdict": "UNCHECKABLE", "evidence": "convertible: execution-time falsifier row"}
    )
    assert not pr.convertible({"verdict": "CONTRADICTED"})
    assert not pr.convertible({"verdict": "UNRESOLVED"})
    assert not pr.convertible(None)


def test_falsifier_task_shape():
    row = pr.falsifier_task("A1", "the  flag\nexists", "C9")
    assert row["id"] == "C9"
    assert row["performer"] == "em"
    assert row["writes"] == []
    assert row["change_kind"] == "test-edit"
    assert "A1" in row["acceptance"] and "the flag exists" in row["acceptance"]
    assert "agent_type" not in row
