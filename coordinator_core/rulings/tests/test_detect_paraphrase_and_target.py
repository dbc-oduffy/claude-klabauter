"""Numeric targets, paraphrased deferrals and silent overrides: both sides of each decision."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.rulings.gate import gate_text
from coordinator_core.rulings import store
from coordinator_core.rulings.contract import Ruling, Target
from coordinator_core.rulings.detect import detect

MM = Ruling(
    id="gate-is-1mm",
    pm_turn="state/sizings/x.yaml R7",
    scope=("tolerance",),
    text="The gate tolerance is 1 mm.",
    recorded="2026-11-21",
    targets=(Target(quantity="gate tolerance", value=1, unit="mm", comparator="<="),),
)
BUDGET = Ruling(
    id="op-budget",
    pm_turn="state/sizings/x.yaml R8",
    scope=("probe",),
    text="The probe budget is 500 ms per op.",
    recorded="2026-11-21",
)
NEG = Ruling(
    id="no-pending-pm",
    pm_turn="state/sizings/x.yaml R2",
    scope=("plan.prep_gate",),
    text="A pending PM ruling is not a state.",
    recorded="2026-11-21",
)


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def _run(text, tmp_path, *rulings):
    return detect(text, list(rulings), tmp_path)


def test_target_roundtrips_and_legacy_record_loads(tmp_path):
    assert store.append(tmp_path, MM)["applied"] == ["gate-is-1mm"]
    assert store.load(tmp_path)[0].targets == MM.targets
    d = BUDGET.to_dict()
    assert "targets" not in d
    assert Ruling.from_dict(d).targets == ()


def test_malformed_target_is_refused_loudly():
    d = MM.to_dict()
    d["targets"] = [{"quantity": "x", "value": "one"}]
    with pytest.raises(ValueError):
        Ruling.from_dict(d)


def test_gate_at_5mm_refuses_against_1mm_target_through_the_gate_entry(tmp_path):
    store.append(tmp_path, MM)
    out = gate_text("Set the gate tolerance to 5 mm for the pilot.", tmp_path)
    assert out["verdict"] == "refuse"
    assert out["contradicted"] == ["gate-is-1mm"]
    assert out["reasons"]["gate-is-1mm"] == ["target_mismatch"]
    assert out["route"] == "coordinator:apm"
    assert gate_text("Set the gate tolerance to 1 mm for the pilot.", tmp_path)["verdict"] == "pass"


@pytest.mark.parametrize(
    "body, verdict",
    [
        ("The gate tolerance is 0.001 m.", "pass"),  # unit-normalised equal
        ("The gate tolerance is 0.5 mm.", "pass"),  # satisfies <=
        ("The gate tolerance is 5000 um.", "refuse"),
        ("The gate_tolerance is 10 mm.", "refuse"),
        ("The gate tolerance is 5 mm, not 1 mm.", "refuse"),
        ("The gate tolerance stays 1 mm, not 5 mm.", "pass"),  # old value mentioned
        ('The row said "gate tolerance 5 mm" earlier.', "pass"),  # quoted mention
        ("The cache size is 5 mm.", "pass"),  # other quantity
        ("The gate tolerance is 5 s.", "pass"),  # other dimension
    ],
)
def test_target_comparison_is_unit_normalised_and_sided(tmp_path, body, verdict):
    assert _run(body, tmp_path, MM)["verdict"] == verdict


def test_equality_comparator_refuses_a_smaller_value(tmp_path):
    eq = Ruling(**{**MM.__dict__, "targets": (Target("gate tolerance", 1, "mm", "=="),)})
    assert _run("The gate tolerance is 0.5 mm.", tmp_path, eq)["contradicted"] == ["gate-is-1mm"]


def test_newer_pm_turn_clears_a_target_mismatch(tmp_path):
    (tmp_path / "turn.md").write_text("pm", encoding="utf-8")
    body = "The gate tolerance is 5 mm.\npm_turn: turn.md 2026-11-22 gate-is-1mm\n"
    assert _run(body, tmp_path, MM)["verdict"] == "pass"
    stale = "The gate tolerance is 5 mm.\npm_turn: turn.md 2026-11-21 gate-is-1mm\n"
    assert _run(stale, tmp_path, MM)["verdict"] == "refuse"


@pytest.mark.parametrize(
    "phrase",
    [
        "we will defer this to the PM",
        "this is parked for the human",
        "punting on it until the PM rules",
        "we leave it to the owner",
        "waiting on the PM",
        "awaiting human input",
        "the PM should weigh in",
        "the threshold is TBD",
        "an open question remains",
    ],
)
def test_paraphrased_deferral_is_seen(tmp_path, phrase):
    out = _run(f"Plan.prep_gate item: {phrase}.", tmp_path, NEG)
    assert out["verdict"] == "refuse"
    assert out["unnamed_deferrals"]
    assert out["deferred"] == ["no-pending-pm"]


@pytest.mark.parametrize(
    "text",
    [
        "Per the PM ruling, plan.prep_gate blocks at 1 mm.",
        "The owner field is set from the record.",
        "Leave the buffer alone; plan.prep_gate is unchanged.",
        'The "open question" heading is removed from plan.prep_gate docs.',
    ],
)
def test_ordinary_prose_is_not_a_deferral(tmp_path, text):
    assert _run(text, tmp_path, NEG)["verdict"] == "pass"


def test_silent_override_value_without_citing_the_id(tmp_path):
    out = _run("The probe budget is 2 s per op.", tmp_path, BUDGET)
    assert out["verdict"] == "refuse"
    assert out["contradicted"] == ["op-budget"]
    assert out["reasons"]["op-budget"] == ["silent_override_value"]
    assert _run("The probe budget is 500 ms per op.", tmp_path, BUDGET)["verdict"] == "pass"
    assert _run("The probe budget is 0.5 s per op.", tmp_path, BUDGET)["verdict"] == "pass"
    assert _run("The probe retries every 2 s.", tmp_path, BUDGET)["verdict"] == "pass"  # no shared quantity


def test_silent_override_value_is_not_reached_outside_scope(tmp_path):
    assert _run("The report budget is 2 s per op.", tmp_path, BUDGET)["verdict"] == "pass"


def test_silent_override_negation_without_citing_the_id(tmp_path):
    out = _run("In plan.prep_gate a pending PM ruling is a valid state.", tmp_path, NEG)
    assert out["contradicted"] == ["no-pending-pm"]
    assert out["reasons"]["no-pending-pm"] == ["silent_override_negation"]
    assert _run("In plan.prep_gate a pending PM ruling is never a state.", tmp_path, NEG)["verdict"] == "pass"
    assert _run("In plan.prep_gate the queue is a valid state.", tmp_path, NEG)["verdict"] == "pass"


def test_silent_override_is_cleared_by_a_newer_pm_turn(tmp_path):
    (tmp_path / "turn.md").write_text("pm", encoding="utf-8")
    body = "The probe budget is 2 s per op.\npm_turn: turn.md 2026-11-22 op-budget\n"
    assert _run(body, tmp_path, BUDGET)["verdict"] == "pass"
