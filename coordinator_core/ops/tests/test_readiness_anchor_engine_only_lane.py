"""plan.readiness_verdict lanes: with no premise-checker output an unevaluable anchor check is a
mechanical repair naming the in-process grammar; with checker output it still blocks."""

import json
from pathlib import Path

from coordinator_core.ops import _anchor_check
from coordinator_core.ops import plan_readiness_verdict as op

from coordinator_core.ops.tests import test_plan_readiness_verdict as base

_PIPE = (
    "anchor_premises:\n  - id: AP1\n    claim: bin has one entry\n    check:\n"
    "      command: ls -d bin | wc -l\n      expect: '1'\n"
)
_GREP_MISMATCH = (
    "anchor_premises:\n  - id: AP1\n    claim: file has three lines\n    check:\n"
    "      command: grep -c x data.txt\n      expect: '3'\n"
)


def test_no_premise_input_repairs_an_unevaluable_check_instead_of_blocking(tmp_path):
    common = base._anchor_plan(tmp_path, _PIPE)
    reply = op._handler({"plan": base.REL}, common)
    base._validate(reply)
    assert (reply["verdict"], reply["pull_class"]) == ("pulled", "mechanical")
    assert reply["blocking"] == [] and reply["blocking_findings"] == 0
    (repair,) = reply["mechanical_repairs"]
    assert (repair["kind"], repair["target"]) == ("anchor-check-not-evaluable", "AP1")
    assert _anchor_check.GRAMMAR in repair["instruction"]
    assert "unsupported shell operator '|'" in repair["instruction"]


def test_premise_param_keeps_the_no_verdict_block(tmp_path):
    common = base._anchor_plan(tmp_path, _PIPE)
    reply = op._handler({"plan": base.REL, "premise": {"rows": []}}, common)
    base._validate(reply)
    assert reply["verdict"] == "judgment"
    assert reply["mechanical_repairs"] == []
    assert any(b.startswith("anchor-premise-no-verdict: AP1") for b in reply["blocking"])
    (ruling,) = [r for r in reply["rulings"] if r["source"] == "premise"]
    assert _anchor_check.GRAMMAR in ruling["ask"] and "premise-checker" in ruling["ask"]


def test_premise_check_review_keeps_the_no_verdict_block(tmp_path):
    common = base._anchor_plan(tmp_path, _PIPE)
    reply = op._handler(
        {"plan": base.REL, "reviews": [{"reviewer": "premise-check", "verdict": "pass"}]}, common
    )
    assert reply["verdict"] == "judgment"
    assert any(b.startswith("anchor-premise-no-verdict: AP1") for b in reply["blocking"])


def test_mismatching_grep_check_still_blocks_contradicted_without_a_checker(tmp_path):
    common = base._anchor_plan(tmp_path, _GREP_MISMATCH)
    (tmp_path / "data.txt").write_text("x\nx\n", encoding="utf-8")
    reply = op._handler({"plan": base.REL}, common)
    base._validate(reply)
    assert reply["verdict"] == "judgment"
    assert any(b.startswith("anchor-premise-CONTRADICTED: AP1") for b in reply["blocking"])
    assert reply["mechanical_repairs"] == []
