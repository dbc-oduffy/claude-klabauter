"""Defer/contradict detector: both sides of each decision, no subprocess."""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from coordinator_core.rulings import detect as detect_module
from coordinator_core.rulings.contract import Ruling
from coordinator_core.rulings.detect import _quantity_re, detect

R = Ruling(
    id="no-pending-pm",
    pm_turn="state/sizings/x.yaml R2",
    scope=("plan.prep_gate", "deferral"),
    text="a pending PM ruling is not a state",
    recorded="2026-11-21",
)


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def _run(text, tmp_path, rulings=(R,)):
    return detect(text, list(rulings), tmp_path)


def test_defer_in_scope_paragraph_refuses_with_ruling_id(tmp_path):
    out = _run("The plan.prep_gate threshold is pending a PM ruling on what to block?", tmp_path)
    assert out["verdict"] == "refuse"
    assert out["deferred"] == ["no-pending-pm"]
    assert out["route"] == "coordinator:apm"
    assert out["unnamed_deferrals"] == []


def test_defer_out_of_scope_with_named_question_passes(tmp_path):
    out = _run("Colour of the logo is pending a PM ruling: red or blue?", tmp_path)
    assert out["verdict"] == "pass"
    assert "route" not in out


def test_unnamed_deferral_refused_without_ruling_match(tmp_path):
    out = _run("Colour of the logo: needs a PM ruling.", tmp_path)
    assert out["verdict"] == "refuse"
    assert out["deferred"] == []
    assert out["unnamed_deferrals"] == ["Colour of the logo: needs a PM ruling."]


@pytest.mark.parametrize(
    "phrase",
    ["pending a PM ruling", "PM to decide", "awaiting PM", "needs a PM ruling", "deferred to the PM"],
)
def test_every_defer_phrase_is_seen(tmp_path, phrase):
    assert _run(f"Open item, {phrase}.", tmp_path)["unnamed_deferrals"]


def test_quoted_defer_phrase_is_a_mention_not_a_deferral(tmp_path):
    assert _run('Remove the "Open, awaiting PM" bullet and `needs a PM ruling` row.', tmp_path)["verdict"] == "pass"
    assert _run('Remove the "bullet"; plan.prep_gate is awaiting PM.', tmp_path)["verdict"] == "refuse"


def test_superseded_ruling_is_not_matched(tmp_path):
    old = Ruling(**{**R.__dict__, "status": "superseded", "superseded_by": "x"})
    out = _run("plan.prep_gate is pending a PM ruling on scope?", tmp_path, [old])
    assert out["verdict"] == "pass"


def test_contradiction_by_id_beside_negation(tmp_path):
    assert _run("We override no-pending-pm here.", tmp_path)["contradicted"] == ["no-pending-pm"]


def test_id_citation_without_negation_passes(tmp_path):
    assert _run("Per no-pending-pm, the stage names its question.", tmp_path)["verdict"] == "pass"


def test_contradiction_by_quoted_text_and_explicit_marker(tmp_path):
    q = _run("We no longer hold that a pending PM ruling is not a state.", tmp_path)
    assert q["contradicted"] == ["no-pending-pm"]
    assert _run("contradicts: no-pending-pm", tmp_path)["contradicted"] == ["no-pending-pm"]


def test_pm_turn_citation_clears_only_when_dated_after_and_resolvable(tmp_path):
    (tmp_path / "turn.md").write_text("pm says", encoding="utf-8")
    body = "plan.prep_gate is pending a PM ruling on scope?\n"
    good = _run(body + "pm_turn: turn.md 2026-11-22 no-pending-pm\n", tmp_path)
    assert good["verdict"] == "pass"
    for bad in (
        "pm_turn: turn.md 2026-11-21 no-pending-pm",  # not after recorded
        "pm_turn: missing.md 2026-11-22 no-pending-pm",  # unresolvable
        "pm_turn: turn.md 2026-11-22 other-id",  # names another ruling
        "pm_turn: ../turn.md 2026-11-22 no-pending-pm",  # escapes the root
        "per the PM",  # bare
    ):
        assert _run(body + bad + "\n", tmp_path)["deferred"] == ["no-pending-pm"], bad


def test_citation_never_clears_an_unnamed_deferral(tmp_path):
    (tmp_path / "turn.md").write_text("x", encoding="utf-8")
    out = _run("plan.prep_gate awaiting PM.\npm_turn: turn.md 2026-11-22 no-pending-pm\n", tmp_path)
    assert out["verdict"] == "refuse"
    assert out["deferred"] == []
    assert out["unnamed_deferrals"]


def test_detect_module_parses_under_python_311_grammar():
    src = Path(detect_module.__file__).read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 11))


@pytest.mark.parametrize("sep", [" ", "_", "-", " - ", "__"])
def test_multi_word_quantity_matches_across_separators(sep):
    rx = _quantity_re("wall thickness")
    assert rx.search(f"the wall{sep}thickness is set")
    assert rx.search("the wallthickness is set") is None
    assert rx.search("the wall thicknesses") is None
