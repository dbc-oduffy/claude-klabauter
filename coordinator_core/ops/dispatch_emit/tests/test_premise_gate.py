"""Tests for the pure epistemic-premise gate evaluator and its sidecar readers."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import premise_gate as pg

DEP = {"depends_on": [{"chunk": "C1", "gate_kind": "epistemic-premise"}]}


def _rows(disposition: str = "coded") -> dict:
    return {"C1": {"id": "C1", "disposition": disposition}, "C2": {"id": "C2", **DEP}}


def _v(verdict, ref="docs/x.md"):
    return {"C1": {"verdict": verdict, "ref": ref, "recorded_at": "2026-10-27"}}


def test_yes_dispatches():
    out = pg.evaluate_premise_gate(_rows()["C2"], _rows(), _v("yes"), True)
    assert out == pg.GateOutcome("dispatch", None)


def test_yaml_bool_spellings_normalise():
    assert pg.evaluate_premise_gate(DEP, _rows(), _v(True), True).action == "dispatch"
    out = pg.evaluate_premise_gate(DEP, _rows(), _v(False), True)
    assert out.action == "close_wont_do" and out.cite["verdict"] == "no"


def test_no_closes_citing_premise_even_when_not_opted_in():
    for opted in (True, False):
        out = pg.evaluate_premise_gate(DEP, _rows(), _v("no"), opted)
        assert out.action == "close_wont_do"
        assert out.cite == {"chunk": "C1", "verdict": "no", "ref": "docs/x.md"}


def test_absent_dispatches_when_opted_in():
    """Run 6467a3: C0 coded without recording its verdict, and every dependent closed wont_do."""
    out = pg.evaluate_premise_gate(DEP, _rows(), {}, True)
    assert out.action == "dispatch"
    assert out.cite == {"chunk": "C1", "reason": "verdict-absent"}


def test_absent_legacy_dispatches_with_reason():
    out = pg.evaluate_premise_gate(DEP, _rows(), {}, False)
    assert out.action == "dispatch"
    assert out.cite == {"chunk": "C1", "reason": "legacy-absent"}


def test_open_premise_waits_regardless_of_verdict():
    for verdicts in ({}, _v("no"), _v("yes")):
        out = pg.evaluate_premise_gate(DEP, _rows("open"), verdicts, True)
        assert out == pg.GateOutcome("wait", {"chunk": "C1"})


def test_non_epistemic_edges_and_unknown_premise_ignored():
    row = {"depends_on": [{"chunk": "C1", "gate_kind": "output-consumption-runtime"},
                          {"chunk": "ZZ", "gate_kind": "epistemic-premise"}]}
    assert pg.evaluate_premise_gate(row, _rows(), {}, True).action == "dispatch"
    assert pg.evaluate_premise_gate({}, _rows(), {}, True).action == "dispatch"


def test_readers_missing_or_bad_sidecar(tmp_path):
    plan = tmp_path / "p.md"
    assert pg.read_premise_verdicts(plan) == {}
    (tmp_path / "p.evidence.yaml").write_text("premises: [1\n", encoding="utf-8")
    assert pg.read_premise_verdicts(plan) == {}
    (tmp_path / "p.evidence.yaml").write_text("premises: 3\n", encoding="utf-8")
    assert pg.read_premise_verdicts(plan) == {}


def test_reader_and_opt_in(tmp_path):
    plan = tmp_path / "p.md"
    plan.write_text("---\ncreated: 2026-01-01\n---\nbody\n", encoding="utf-8")
    assert pg.plan_opted_in(plan) is False
    (tmp_path / "p.evidence.yaml").write_text(
        "premises:\n  C1: {verdict: 'no', ref: abc, recorded_at: t}\n", encoding="utf-8")
    assert pg.read_premise_verdicts(plan)["C1"]["verdict"] == "no"
    assert pg.plan_opted_in(plan) is True


def test_opt_in_by_created_date(tmp_path):
    plan = tmp_path / "p.md"
    plan.write_text("---\ncreated: 2026-10-27\n---\n", encoding="utf-8")
    assert pg.plan_opted_in(plan) is True
    plan.write_text("---\ncreated: '2026-10-26'\n---\n", encoding="utf-8")
    assert pg.plan_opted_in(plan) is False
    assert pg.plan_opted_in(tmp_path / "missing.md") is False


def test_brief_line_names_verb():
    assert "premise-record" in pg.PREMISE_RECORD_BRIEF_LINE
    assert "\n" not in pg.PREMISE_RECORD_BRIEF_LINE
