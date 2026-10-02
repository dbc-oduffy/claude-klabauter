"""discharge_clauses: accepted pm/ceo exit criterion discharges a gate on the plan's own DR."""

import pytest

from coordinator_core.ops.dispatch_emit.self_dr_discharge import discharge_clauses


def _rows(dr_path="docs/decisions/x.md"):
    return [
        {"id": "C3", "writes": [dr_path]},
        {"id": "C4", "writes": ["a.py"],
         "depends_on": [{"chunk": "C3", "gate_kind": "epistemic-premise"}]},
    ]


def _sizing(mode="pm", quote="ship it"):
    return {"exit_criterion": {
        "statement": "the outcome",
        "accepted": {"pm_quote": quote, "on": "2026-10-02", "mode": mode},
    }}


def test_pm_mode_discharges_dependent():
    out = discharge_clauses(_rows(), _sizing())
    assert list(out) == ["C4"]
    for needle in ("docs/decisions/x.md", "C3", "ship it", "the outcome", "pm", "2026-10-02"):
        assert needle in out["C4"]


@pytest.mark.parametrize("sizing", [
    _sizing(mode="hands-on"),
    {"exit_criterion": {"statement": "s", "accepted": None}},
    None,
    _sizing(quote="  "),
])
def test_no_discharge(sizing):
    assert discharge_clauses(_rows(), sizing) == {}


def test_non_decision_predecessor():
    assert discharge_clauses(_rows("src/x.md"), _sizing()) == {}


def test_windows_separator():
    assert list(discharge_clauses(_rows("docs\\decisions\\x.md"), _sizing())) == ["C4"]


def test_ceo_mode_behaves_as_pm():
    assert list(discharge_clauses(_rows(), _sizing(mode="ceo"))) == ["C4"]
