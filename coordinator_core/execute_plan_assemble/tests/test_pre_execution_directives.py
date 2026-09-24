"""`pre_execution_directives` — pure function, zero I/O, zero spawns.

Negative-spec:
    - Does NOT spawn anything: no `pickup-assemble`, no `session-claim-cli`,
      no `emit-dispatch-workflow`. This module builds the directive list;
      dispatching it is C2's job.
    - Does NOT assert against C2's `_CLI_DISPATCH` table — C2 does not exist
      yet in this chunk's footprint. Only the shape this module itself owns
      is pinned here: order, `--for-execution`, the autonomous shape, and
      `depends_on` on each directive.
"""

from __future__ import annotations

from coordinator_core.execute_plan_assemble.pre_execution import (
    pre_execution_directives,
)

PLAN_PATH = "docs/plans/2026-09-11-the-execute-plan-pre-execution-chain-emi.md"


def test_default_order_is_d1_d2_d3_d4():
    directives, _judgment_points = pre_execution_directives(PLAN_PATH)
    assert [d["id"] for d in directives] == ["d1", "d2", "d3", "d4"]


def test_autonomous_omits_d1_and_d2():
    directives, judgment_points = pre_execution_directives(
        PLAN_PATH, autonomous=True
    )
    assert [d["id"] for d in directives] == ["d3", "d4"]
    # Same judgment points regardless of autonomous — the gates still gate.
    assert [jp["id"] for jp in judgment_points] == [
        "j-remaining-context",
        "j-executability-gate",
        "j-roadmap-execution-gate",
        "j-wave-map",
    ]


def test_d3_claims_for_execution():
    directives, _judgment_points = pre_execution_directives(PLAN_PATH)
    d3 = next(d for d in directives if d["id"] == "d3")
    assert d3["cli"] == "session-claim-cli"
    assert "--for-execution" in d3["args"]
    assert d3["args"][0] == "claim-plan"
    assert d3["args"][1] == "2026-09-11-the-execute-plan-pre-execution-chain-emi"


def test_d1_and_d2_carry_no_depends_on():
    directives, _judgment_points = pre_execution_directives(PLAN_PATH)
    d1 = next(d for d in directives if d["id"] == "d1")
    d2 = next(d for d in directives if d["id"] == "d2")
    assert d1["depends_on"] is None
    assert d2["depends_on"] is None


def test_d3_and_d4_depend_on_the_three_gates():
    directives, judgment_points = pre_execution_directives(PLAN_PATH)
    judgment_point_ids = {jp["id"] for jp in judgment_points}
    d3 = next(d for d in directives if d["id"] == "d3")
    d4 = next(d for d in directives if d["id"] == "d4")

    assert d3["depends_on"]
    assert set(d3["depends_on"]) <= judgment_point_ids
    assert d4["depends_on"]
    assert set(d4["depends_on"]) <= judgment_point_ids

    # d4 additionally depends on the wave-map point; d3 does not.
    assert "j-wave-map" in d4["depends_on"]
    assert "j-wave-map" not in d3["depends_on"]


def test_d2_authorizes_the_typed_command():
    directives, _judgment_points = pre_execution_directives(PLAN_PATH)
    d2 = next(d for d in directives if d["id"] == "d2")
    assert d2["cli"] == "review-exec-auth-stamp"
    assert d2["args"] == [
        "authorize-invocation",
        PLAN_PATH,
        "--typed-command",
        "/execute-plan",
    ]


def test_judgment_points_are_untrusted_gates_with_no_recommendation():
    _directives, judgment_points = pre_execution_directives(PLAN_PATH)
    assert len(judgment_points) == 4
    for jp in judgment_points:
        assert jp["recommendation"] is None
