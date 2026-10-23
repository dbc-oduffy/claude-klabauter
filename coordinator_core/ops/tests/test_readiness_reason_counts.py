"""readiness_verdict(): the reason's "N blocking, M advisory" prefix equals the integer fields in
every finding mix, and every anchor finding is blocking and listed in `blocking`."""

from __future__ import annotations

import re

from coordinator_core.ops.plan_readiness_verdict import readiness_verdict

_GATE = {"plan": "p.md", "verdict": "PREPPED", "withheld_rows": [], "classes": {}, "message": ""}
_ANCHORS = [f"anchor-premise-no-verdict: a{i}" for i in range(6)]
_WARN = {"reviewer": "r", "verdict": "WARN"}
_COUNTS = re.compile(r"^needs an agent: (\d+) blocking, (\d+) advisory: ")


def _assert_consistent(reply: dict) -> tuple:
    match = _COUNTS.match(reply["reason"])
    assert match, reply["reason"]
    assert (int(match[1]), int(match[2])) == (
        reply["blocking_findings"],
        reply["advisory_findings"],
    )
    return reply["blocking_findings"], reply["advisory_findings"]


def test_six_anchor_findings_and_one_warn_review():
    reply = readiness_verdict(_GATE, None, [_WARN], anchor_findings=list(_ANCHORS))
    assert reply["blocking_findings"] == 6
    assert reply["advisory_findings"] == 1
    assert reply["reason"].startswith("needs an agent: 6 blocking, 1 advisory: ")
    assert set(_ANCHORS) <= set(reply["blocking"])
    _assert_consistent(reply)


def test_anchor_only_mix():
    reply = readiness_verdict(_GATE, None, [], anchor_findings=list(_ANCHORS))
    assert _assert_consistent(reply) == (6, 0)
    assert len(reply["blocking"]) == 6


def test_premise_rows_only_mix():
    rows = [
        {"verdict": "UNRESOLVED", "citation": f"claim {i}"} for i in range(3)
    ]
    reply = readiness_verdict(_GATE, None, [], premise_rows=rows)
    assert _assert_consistent(reply) == (3, 0)


def test_coverage_only_mix():
    coverage = {"verdict": "BLOCKED-SURFACE-TO-PM", "judgmentFindings": ["a", "b"]}
    reply = readiness_verdict(_GATE, coverage, [])
    assert _assert_consistent(reply) == (2, 0)
    advisory = {"verdict": "WARN", "judgmentFindings": ["a", "b", "c"]}
    reply = readiness_verdict(_GATE, advisory, [])
    assert _assert_consistent(reply) == (0, 3)
