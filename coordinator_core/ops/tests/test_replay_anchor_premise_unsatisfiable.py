"""Replay of ladder defect #1: a premise the gate cannot evaluate in its lane (not evaluable
in-process, or refused by the census screen) is a note naming its anchor id, never a repair, pull
or halt, with or without a premise-checker."""

import pytest

from coordinator_core.ops import _anchor_premise as ap
from coordinator_core.ops import plan_readiness_verdict as prv

_UNEVALUABLE = [
    {"id": "AP-PIPE", "claim": "pipe", "check": {"command": "grep -c hello a.txt | wc -l", "expect": "1"}},
    {"id": "AP-GREP-N", "claim": "flag", "check": {"command": "grep -n hello a.txt", "expect": "1"}},
    {"id": "AP-NOT-GREP", "claim": "not grep", "check": {"command": "ls a.txt", "expect": "a.txt"}},
]
_REFUSED = [
    {"id": "P2", "claim": "typecheck", "check": {"command": "pnpm typecheck", "expect": "0"}},
    {"id": "P3", "claim": "pipe", "check": {"command": "pnpm test | wc -l", "expect": "0"}},
]
_MALFORMED = [{"id": "P9", "check": {"command": "grep -c hello a.txt", "expect": "1"}}]


def _verdict(tmp_path, entries, checker_ran):
    (tmp_path / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    anchors, defects = ap.parse_anchors({"anchor_premises": entries}, tmp_path)
    verdicts = ap.anchor_verdicts(anchors, [], [], "", tmp_path)
    findings = list(defects) + ap.anchor_findings(anchors, [], [], "", tmp_path, verdicts)
    return prv.readiness_verdict(
        {"verdict": "PREPPED", "classes": {}}, None, [], tmp_path, [], None, None, None, None, "",
        findings, verdicts, checker_ran,
    )


@pytest.mark.parametrize("checker_ran", [True, False])
@pytest.mark.parametrize("entries", [_UNEVALUABLE, _REFUSED], ids=["unevaluable", "screen-refused"])
def test_unsatisfiable_premise_is_a_note_not_a_pull(tmp_path, entries, checker_ran):
    out = _verdict(tmp_path, entries, checker_ran)
    assert out["verdict"] == "ready", out
    assert out["mechanical_repairs"] == [] and out["blocking"] == []
    for entry in entries:
        assert entry["id"] in out["reason"], out


# The r41/r40 plan files are not on disk; each fixture reproduces that run's refused premise shape.
_NAMED_PLANS = {
    "L-r41": [{"id": "AP13", "claim": "bin dir", "check": {"command": "ls -d bin | wc -l", "expect": "1"}}],
    "XL-r41": [
        {"id": "AP13", "claim": "bin dir", "check": {"command": "ls -d bin | wc -l", "expect": "1"}},
        {"id": "P2", "claim": "typecheck", "check": {"command": "pnpm typecheck", "expect": "0"}},
    ],
    "XXL-r40": [
        {"id": "P2", "claim": "pipe", "check": {"command": "grep -c hello a.txt | wc -l", "expect": "1"}},
        {"id": "P3", "claim": "tests", "check": {"command": "pnpm test | wc -l", "expect": "0"}},
    ],
}


@pytest.mark.parametrize("checker_ran", [True, False])
@pytest.mark.parametrize("plan", ["L-r41", "XL-r41", "XXL-r40"])
def test_named_run_plan_not_refused_by_anchor_gate(tmp_path, plan, checker_ran):
    out = _verdict(tmp_path, _NAMED_PLANS[plan], checker_ran)
    assert out["verdict"] == "ready", out
    assert not any("anchor-premise" in b for b in out["blocking"]), out


@pytest.mark.parametrize("checker_ran", [True, False])
def test_malformed_premise_still_blocks(tmp_path, checker_ran):
    out = _verdict(tmp_path, _MALFORMED, checker_ran)
    assert out["verdict"] == "judgment", out
    assert any("P9" in b for b in out["blocking"]), out
