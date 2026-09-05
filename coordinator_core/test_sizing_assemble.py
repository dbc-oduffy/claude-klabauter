"""
coordinator_core.test_sizing_assemble — co-located pytest for
coordinator_core.sizing_assemble.

Covers: express-lane short-circuit (D3), appetite-conform auto-route,
appetite-exceeded fork surfacing (never auto-resolved, AC5), symmetric
resize in BOTH directions (collapse and raise, Finding 2) including the
route_boundary_crossed detent when a resize changes routing tier, and the
D5 shape-entry conditions (large+jtbd_unclear, well-trodden+step-change,
and the large-but-clear skip-shape case).

Also covers the 2026-07-30 route-table re-cut: S -> spec-dispatch (distinct
from XS's dispatch), XL -> pm-decision (with the pm_decision_pending
detent, superseding the old straight-to-roadmap XL route), `xl_exit` staying
None on every path (engineered identically to `fork`), the D5 gate still
winning over pm-decision, the combined appetite_exceeded + pm-decision
next_move, and a spec-dispatch CLI smoke test.

Also covers the 2026-08-05 premise-provenance detent (cross-repo memo
2026-08-05-doe-claude-em-premise-provenance-detent-sizing-assemble.md):
`premise_unproven` / `premise_not_applicable` firing on resized L/XL across
all three non-plan routes, NOT firing at XS/S/M nor for
executed/unrecorded/None, unconditional validation on the express_lane
path, the advisory wording in `next_move`, and a provenance-invariance
property test asserting `route()`'s output is identical between `read` and
`executed` (and `not-applicable` and `executed`) on every key except
`detents`/`next_move` — the point-check gap the memo's reviewer named
(the shape-gate disjunction at `_LARGE_TSHIRTS and jtbd_unclear or
well_trodden_step_change`, and the combined appetite_exceeded +
pm-decision branch).

Run: cd /Users/example-operator/X/claude-klabauter && python3 -m pytest coordinator_core/test_sizing_assemble.py -q
"""
from __future__ import annotations

import itertools

import pytest

import coordinator_core.sizing_assemble as sa


def test_express_lane_short_circuits_to_dispatch():
    decision = sa.route(appetite="small", estimate={"tshirt": "XL"}, express_lane=True)
    assert decision["route"] == "dispatch"
    assert decision["detents"] == []
    assert decision["fork"] is None


def test_conforming_estimate_auto_routes_no_fork():
    decision = sa.route(appetite="medium", estimate={"tshirt": "M"})
    assert decision["route"] == "plan"
    assert decision["fork"] is None
    assert "appetite_conform" in decision["detents"]


def test_small_tshirt_routes_spec_dispatch():
    decision = sa.route(appetite="small", estimate={"tshirt": "S"})
    assert decision["route"] == "spec-dispatch"
    assert decision["fork"] is None
    assert decision["xl_exit"] is None


def test_small_tshirt_route_distinct_from_xs():
    xs_decision = sa.route(appetite="small", estimate={"tshirt": "XS"})
    s_decision = sa.route(appetite="small", estimate={"tshirt": "S"})
    assert xs_decision["route"] == "dispatch"
    assert s_decision["route"] == "spec-dispatch"
    assert xs_decision["route"] != s_decision["route"]


def test_appetite_exceeded_surfaces_fork_not_auto_resolved():
    decision = sa.route(appetite="small", estimate={"tshirt": "L"})
    # appetite_exceeded is the sole divergence signal; fork stays None from
    # route() always — it is the skill's resolution slot, filled only once
    # the PM has picked cut_to_fit vs raise_appetite (Finding 2).
    assert "appetite_exceeded" in decision["detents"]
    assert decision["fork"] is None
    assert decision["route"] in sa.ROUTE_ENUM


def test_symmetric_resize_collapses_over_read():
    baseline = sa.route(appetite="large", estimate={"tshirt": "L"})
    collapsed = sa.route(appetite="large", estimate={"tshirt": "L"}, probe_signal="collapse")
    assert collapsed["resolved_estimate"]["tshirt"] == "M"
    assert baseline["resolved_estimate"]["tshirt"] == "L"


def test_symmetric_resize_raises_under_read():
    raised = sa.route(appetite="large", estimate={"tshirt": "S"}, probe_signal="raise")
    assert raised["resolved_estimate"]["tshirt"] == "M"
    # S alone would dispatch; the raised M reads plan — the resize caught
    # the under-read and moved it to heavier ceremony (Finding 2).
    assert raised["route"] == "plan"
    assert "route_boundary_crossed" in raised["detents"]


def test_raise_can_cross_appetite_ceiling_into_a_fork():
    # A small-appetite S under-read raised to M by the probe should surface
    # the appetite/estimate divergence rather than silently plan-routing.
    # The divergence signal is the appetite_exceeded detent; fork stays
    # None from route() always (Finding 2 — fork is the skill's resolution
    # slot, not an engine placeholder).
    decision = sa.route(appetite="small", estimate={"tshirt": "S"}, probe_signal="raise")
    assert decision["resolved_estimate"]["tshirt"] == "M"
    assert "appetite_exceeded" in decision["detents"]
    assert decision["fork"] is None


def test_large_and_jtbd_unclear_routes_shape():
    decision = sa.route(
        appetite="large",
        estimate={"tshirt": "L"},
        jtbd_unclear=True,
    )
    assert decision["route"] == "shape"
    assert "scope_boundary_acknowledged" in decision["detents"]


def test_large_and_well_trodden_step_change_routes_shape():
    decision = sa.route(
        appetite="large",
        estimate={"tshirt": "XL"},
        well_trodden_step_change=True,
    )
    assert decision["route"] == "shape"


def test_well_trodden_step_change_routes_shape_at_non_large_tshirt():
    # D5 condition (b) is spec'd size-independent (plan D5; module docstring)
    # — well_trodden_step_change alone must route to shape even at S/M,
    # not just at L/XL. This is the exact gap Finding 1 identified: the
    # prior test only exercised well_trodden_step_change at XL, which could
    # not distinguish "size-gated" from "size-independent" behavior.
    decision = sa.route(
        appetite="small",
        estimate={"tshirt": "S"},
        well_trodden_step_change=True,
    )
    assert decision["route"] == "shape"
    assert "scope_boundary_acknowledged" in decision["detents"]


def test_xl_but_clear_skips_shape_routes_pm_decision():
    decision = sa.route(appetite="large", estimate={"tshirt": "XL"})
    assert decision["route"] == "pm-decision"
    assert decision["fork"] is None
    assert decision["xl_exit"] is None
    assert "pm_decision_pending" in decision["detents"]


def test_xl_route_sets_pm_decision_pending_detent():
    decision = sa.route(appetite="large", estimate={"tshirt": "XL"})
    assert "pm_decision_pending" in decision["detents"]


def test_d5_gate_still_wins_over_pm_decision():
    # XL + jtbd_unclear must still route shape — the D5 gate is unchanged
    # and wins over the new pm-decision base route exactly as it won over
    # the old roadmap base route.
    decision = sa.route(
        appetite="large",
        estimate={"tshirt": "XL"},
        jtbd_unclear=True,
    )
    assert decision["route"] == "shape"
    assert "pm_decision_pending" not in decision["detents"]


def test_xl_exit_is_none_on_every_path():
    express = sa.route(appetite="small", estimate={"tshirt": "XL"}, express_lane=True)
    assert express["xl_exit"] is None

    pm_decision = sa.route(appetite="large", estimate={"tshirt": "XL"})
    assert pm_decision["xl_exit"] is None

    dispatch = sa.route(appetite="small", estimate={"tshirt": "XS"})
    assert dispatch["xl_exit"] is None

    shaped = sa.route(appetite="large", estimate={"tshirt": "L"}, jtbd_unclear=True)
    assert shaped["xl_exit"] is None


def test_appetite_exceeded_and_pm_decision_combined_next_move_mentions_both_forks():
    # small appetite (ceiling S) + XL estimate is both appetite_exceeded AND
    # pm-decision — the combined next_move must mention BOTH forks so the EM
    # makes one PM ask, not two.
    decision = sa.route(appetite="small", estimate={"tshirt": "XL"})
    assert decision["route"] == "pm-decision"
    assert "appetite_exceeded" in decision["detents"]
    assert "pm_decision_pending" in decision["detents"]
    assert "cut_to_fit" in decision["next_move"]
    assert "raise_appetite" in decision["next_move"]
    assert "xl_exit" in decision["next_move"]


# --- C5: sixth-notch XXL/goal-setting (2026-08-07 sizing-ladder-xxl-notch-
# and-goal-setting-route plan) --------------------------------------------


def test_xxl_routes_goal_setting_when_d5_gate_does_not_fire():
    # AC1 — qualified: fires ONLY when the D5 shape-entry gate does not
    # trigger (jtbd_unclear not set, well_trodden_step_change false).
    decision = sa.route(estimate={"tshirt": "XXL"})
    assert decision["route"] == "goal-setting"


def test_xxl_with_jtbd_unclear_routes_shape_not_goal_setting():
    # AC11 — the D5 shape-entry gate PRE-EMPTS `_BASE_ROUTE_BY_TSHIRT`
    # before it is consulted (finding 3): an XXL with jtbd_unclear=True
    # resolves shape, never goal-setting, and carries
    # scope_boundary_acknowledged. Named positive case, not left implicit.
    decision = sa.route(estimate={"tshirt": "XXL"}, jtbd_unclear=True)
    assert decision["route"] == "shape"
    assert decision["route"] != "goal-setting"
    assert "scope_boundary_acknowledged" in decision["detents"]


def test_xxl_weight_and_validation():
    # AC2/AC3.
    assert sa.TSHIRT_WEIGHT["XXL"] == 32
    sa._validate_tshirt("XXL")  # must not raise
    assert "goal-setting" in sa.ROUTE_ENUM


def test_goal_setting_in_route_enum_and_defensive_raise_unreachable():
    # AC3 — the `resolved_route not in ROUTE_ENUM` defensive raise never
    # fires for an XXL resolution, because `goal-setting` is a member.
    assert "goal-setting" in sa.ROUTE_ENUM
    decision = sa.route(estimate={"tshirt": "XXL"})
    assert decision["route"] in sa.ROUTE_ENUM


# --- AC4: band-constant audit, one assertion per constant, including the
# deliberate non-change --------------------------------------------------


def test_xxl_fires_premise_detents_large_tshirts_widened():
    # `_LARGE_TSHIRTS` widened to include XXL.
    decision = sa.route(
        estimate={"tshirt": "XXL"}, premise_provenance="read"
    )
    assert "premise_unproven" in decision["detents"]


def test_xxl_fires_d5_shape_gate_large_tshirts_widened():
    # `_LARGE_TSHIRTS` widened -- XXL + jtbd_unclear fires the shape gate
    # (also asserted by test_xxl_with_jtbd_unclear_routes_shape_not_goal_setting
    # above; this one names the constant explicitly per AC4's "one assertion
    # per band constant" requirement).
    decision = sa.route(estimate={"tshirt": "XXL"}, jtbd_unclear=True)
    assert "scope_boundary_acknowledged" in decision["detents"]


def test_xxl_post_size_prompt_pending_tshirts_widened():
    # `_POST_SIZE_PROMPT_TSHIRTS` widened to include XXL.
    decision = sa.route(estimate={"tshirt": "XXL"})
    assert "post_size_prompt_pending" in decision["detents"]


def test_xxl_against_large_appetite_still_reads_appetite_exceeded():
    # `_APPETITE_CEILING_TSHIRT` deliberately NOT widened -- the negative
    # case that catches a later "helpful" fix. XXL against the largest
    # budget ("large" -> XL ceiling) still reads appetite_exceeded.
    decision = sa.route(appetite="large", estimate={"tshirt": "XXL"})
    assert "appetite_exceeded" in decision["detents"]
    assert "appetite_conform" not in decision["detents"]


def test_xxl_against_large_appetite_next_move_carries_goal_setting_framing():
    # Review-integrator P1 fix (coordinator:code-reviewer 2026-08-07): the
    # appetite_exceeded arm used to shadow the goal-setting arm entirely for
    # this cell -- both detents fire, but next_move only ever surfaced the
    # cut/raise fork text. Pin that both the closed fork (AC6: volunteered
    # appetite keeps the closed fork, never the open question) AND the
    # goal-setting/PM-gated framing survive in the operator-facing text.
    decision = sa.route(appetite="large", estimate={"tshirt": "XXL"})
    assert "appetite_exceeded" in decision["detents"]
    assert "goal_setting_pm_gated" in decision["detents"]
    next_move = decision["next_move"]
    assert "cut_to_fit" in next_move and "raise_appetite" in next_move
    assert "goal-setting" in next_move
    assert "PM-gated" in next_move


# --- AC5: xxl_unprobed -- BOTH reachable cells (Key-decision section) ------


def test_xxl_unprobed_fires_on_promotion_from_xl_with_empty_evidence():
    # Cell (a): --tshirt XL --probe-signal raise, empty scout_evidence ->
    # resolves XXL (promotion) AND emits xxl_unprobed.
    decision = sa.route(
        estimate={"tshirt": "XL"}, probe_signal="raise", scout_evidence=[]
    )
    assert decision["resolved_estimate"]["tshirt"] == "XXL"
    assert "xxl_unprobed" in decision["detents"]


def test_xxl_unprobed_fires_on_noop_clamp_from_xxl_with_empty_evidence():
    # Cell (b): --tshirt XXL --probe-signal raise, empty scout_evidence ->
    # resolves XXL (no-op clamp, resized == tshirt) AND still emits
    # xxl_unprobed -- the missed cell DoE's reply named.
    decision = sa.route(
        estimate={"tshirt": "XXL"}, probe_signal="raise", scout_evidence=[]
    )
    assert decision["resolved_estimate"]["tshirt"] == "XXL"
    assert "xxl_unprobed" in decision["detents"]


def test_xxl_unprobed_absent_on_promotion_with_evidence():
    decision = sa.route(
        estimate={"tshirt": "XL"},
        probe_signal="raise",
        scout_evidence=["docs/wiki/foo.md"],
    )
    assert decision["resolved_estimate"]["tshirt"] == "XXL"
    assert "xxl_unprobed" not in decision["detents"]


def test_xxl_unprobed_absent_on_noop_clamp_with_evidence():
    decision = sa.route(
        estimate={"tshirt": "XXL"},
        probe_signal="raise",
        scout_evidence=["docs/wiki/foo.md"],
    )
    assert decision["resolved_estimate"]["tshirt"] == "XXL"
    assert "xxl_unprobed" not in decision["detents"]


def test_only_raise_reaches_xxl_collapse_at_xxl_resolves_xl_no_xxl_unprobed():
    # Fourth case pinning the "only raise reaches XXL" premise: a collapse
    # probe at XXL resolves XL, not XXL, and never emits xxl_unprobed.
    decision = sa.route(estimate={"tshirt": "XXL"}, probe_signal="collapse")
    assert decision["resolved_estimate"]["tshirt"] == "XL"
    assert "xxl_unprobed" not in decision["detents"]


# --- AC6: xxl_unprobed is advisory-only -- never alters route/fork/xl_exit/
# resolved_estimate. Modeled on the premise-provenance invariance shape.


def test_xxl_unprobed_present_absent_leaves_everything_but_detents_next_move_identical():
    with_advisory = sa.route(
        estimate={"tshirt": "XL"}, probe_signal="raise", scout_evidence=[]
    )
    without_advisory = sa.route(
        estimate={"tshirt": "XL"},
        probe_signal="raise",
        scout_evidence=["docs/wiki/foo.md"],
    )
    assert "xxl_unprobed" in with_advisory["detents"]
    assert "xxl_unprobed" not in without_advisory["detents"]
    # AC6: xxl_unprobed never alters route/fork/xl_exit/resolved_estimate.
    # scout_evidence itself is deliberately varied between these two calls
    # (empty vs non-empty) to trigger/suppress the detent, so it is excluded
    # here alongside detents/next_move -- it is the predicate's own input,
    # not an output AC6 governs.
    for key in ("route", "fork", "xl_exit", "resolved_estimate"):
        assert with_advisory[key] == without_advisory[key], key


def test_xl_exit_enum_byte_identical_to_pre_plan_value():
    # AC9 -- cheap tripwire on the one thing the memo explicitly asked us
    # not to touch. `split` stays a member of XL_EXIT_ENUM (the vocabulary
    # word) even though this plan's C2 drops `split` from the pm-decision
    # next_move TEXT -- the vocabulary value and the offered exit are
    # distinct; this test pins the former only.
    assert sa.XL_EXIT_ENUM == ("split", "shape", "roadmap", "accept_multi_session")


def test_unknown_appetite_raises_usage_error():
    with pytest.raises(sa.SizingAssembleError):
        sa.route(appetite="huge", estimate={"tshirt": "M"})


def test_unknown_tshirt_raises_usage_error():
    # Fixture updated 2026-08-07 (sizing-ladder-xxl-notch-and-goal-setting-
    # route plan, C5): "XXL" is now a valid ladder member (C1), so it can no
    # longer serve as the junk fixture proving an unknown tshirt raises.
    # "XXXL" preserves the test's original intent -- a genuinely unknown
    # tshirt still raises SizingAssembleError.
    with pytest.raises(sa.SizingAssembleError):
        sa.route(appetite="medium", estimate={"tshirt": "XXXL"})


def test_route_is_read_only_does_not_mutate_inputs():
    estimate = {"tshirt": "M"}
    scout_evidence = ["docs/wiki/foo.md"]
    sa.route(appetite="medium", estimate=estimate, scout_evidence=scout_evidence)
    assert estimate == {"tshirt": "M"}
    assert scout_evidence == ["docs/wiki/foo.md"]


def test_cli_main_smoke(capsys):
    exit_code = sa.main(["--appetite", "medium", "--tshirt", "M"])
    assert exit_code == sa.EXIT_OK
    out = capsys.readouterr().out
    assert '"route": "plan"' in out


def test_cli_main_usage_error_on_missing_args(capsys):
    exit_code = sa.main(["--appetite", "medium"])
    assert exit_code == sa.EXIT_USAGE


def test_cli_main_express_lane_flag(capsys):
    exit_code = sa.main(["--appetite", "small", "--tshirt", "XL", "--express-lane"])
    assert exit_code == sa.EXIT_OK
    out = capsys.readouterr().out
    assert '"route": "dispatch"' in out
    assert '"detents": []' in out


def test_cli_main_probe_signal_flag(capsys):
    exit_code = sa.main(
        ["--appetite", "large", "--tshirt", "L", "--probe-signal", "collapse"]
    )
    assert exit_code == sa.EXIT_OK
    out = capsys.readouterr().out
    assert '"tshirt": "M"' in out


def test_cli_main_jtbd_unclear_flag(capsys):
    exit_code = sa.main(
        ["--appetite", "large", "--tshirt", "L", "--jtbd-unclear"]
    )
    assert exit_code == sa.EXIT_OK
    out = capsys.readouterr().out
    assert '"route": "shape"' in out


def test_cli_main_well_trodden_step_change_flag(capsys):
    exit_code = sa.main(
        ["--appetite", "small", "--tshirt", "S", "--well-trodden-step-change"]
    )
    assert exit_code == sa.EXIT_OK
    out = capsys.readouterr().out
    assert '"route": "shape"' in out


def test_cli_main_spec_dispatch_smoke(capsys):
    exit_code = sa.main(["--appetite", "small", "--tshirt", "S"])
    assert exit_code == sa.EXIT_OK
    out = capsys.readouterr().out
    assert '"route": "spec-dispatch"' in out
    assert '"xl_exit": null' in out


def test_cli_main_scout_evidence_flag(capsys):
    exit_code = sa.main(
        [
            "--appetite",
            "medium",
            "--tshirt",
            "M",
            "--scout-evidence",
            "docs/wiki/foo.md",
            "--scout-evidence",
            "docs/wiki/bar.md",
        ]
    )
    assert exit_code == sa.EXIT_OK
    out = capsys.readouterr().out
    assert "docs/wiki/foo.md" in out
    assert "docs/wiki/bar.md" in out


# --- Premise-provenance detent (2026-08-05 memo) ---------------------------


@pytest.mark.parametrize(
    "tshirt,expected_route",
    [("L", "plan"), ("XL", "pm-decision"), ("XXL", "goal-setting")],
)
def test_premise_unproven_fires_at_large_plan_routed(tshirt, expected_route):
    decision = sa.route(
        appetite="large", estimate={"tshirt": tshirt}, premise_provenance="read"
    )
    assert decision["route"] == expected_route
    assert "premise_unproven" in decision["detents"]


def test_premise_unproven_fires_on_shape_routed_large():
    decision = sa.route(
        appetite="large",
        estimate={"tshirt": "L"},
        jtbd_unclear=True,
        premise_provenance="read",
    )
    assert decision["route"] == "shape"
    assert "premise_unproven" in decision["detents"]


def test_premise_unproven_fires_on_pm_decision_routed_large():
    decision = sa.route(
        appetite="large", estimate={"tshirt": "XL"}, premise_provenance="read"
    )
    assert decision["route"] == "pm-decision"
    assert "premise_unproven" in decision["detents"]


def test_premise_not_applicable_fires_at_large_plan_routed():
    decision = sa.route(
        appetite="large", estimate={"tshirt": "L"}, premise_provenance="not-applicable"
    )
    assert decision["route"] == "plan"
    assert "premise_not_applicable" in decision["detents"]


def test_premise_not_applicable_fires_on_shape_routed_large():
    decision = sa.route(
        appetite="large",
        estimate={"tshirt": "XL"},
        well_trodden_step_change=True,
        premise_provenance="not-applicable",
    )
    assert decision["route"] == "shape"
    assert "premise_not_applicable" in decision["detents"]


def test_premise_not_applicable_fires_on_pm_decision_routed_large():
    decision = sa.route(
        appetite="large", estimate={"tshirt": "XL"}, premise_provenance="not-applicable"
    )
    assert decision["route"] == "pm-decision"
    assert "premise_not_applicable" in decision["detents"]


@pytest.mark.parametrize("tshirt", ["XS", "S"])
def test_premise_detents_do_not_fire_below_large(tshirt):
    read_decision = sa.route(
        appetite="large", estimate={"tshirt": tshirt}, premise_provenance="read"
    )
    na_decision = sa.route(
        appetite="large", estimate={"tshirt": tshirt}, premise_provenance="not-applicable"
    )
    assert "premise_unproven" not in read_decision["detents"]
    assert "premise_not_applicable" not in na_decision["detents"]


@pytest.mark.parametrize("provenance", ["executed", "unrecorded", None])
def test_premise_detents_do_not_fire_for_non_read_non_not_applicable(provenance):
    for tshirt in ("L", "XL"):
        decision = sa.route(
            appetite="large",
            estimate={"tshirt": tshirt},
            premise_provenance=provenance,
        )
        assert "premise_unproven" not in decision["detents"]
        assert "premise_not_applicable" not in decision["detents"]


def test_premise_unproven_fires_at_m_plan_routed():
    # M is in _PREMISE_DETENT_TSHIRTS but NOT in _LARGE_TSHIRTS — the gap
    # this dispatch closes (doe-claude-em was hand-reading M for this).
    decision = sa.route(
        appetite="large", estimate={"tshirt": "M"}, premise_provenance="read"
    )
    assert decision["route"] == "plan"
    assert "premise_unproven" in decision["detents"]


def test_premise_not_applicable_fires_at_m_plan_routed():
    decision = sa.route(
        appetite="large", estimate={"tshirt": "M"}, premise_provenance="not-applicable"
    )
    assert decision["route"] == "plan"
    assert "premise_not_applicable" in decision["detents"]


def test_m_jtbd_unclear_still_routes_to_plan_not_shape():
    # Regression guard: _LARGE_TSHIRTS (which gates the shape-route
    # condition) must NOT have been widened to include "M" as a side effect
    # of closing the premise-detent gap above.
    decision = sa.route(
        appetite="large",
        estimate={"tshirt": "M"},
        jtbd_unclear=True,
    )
    assert decision["route"] == "plan"


def test_premise_unproven_keys_on_resized_size_not_pre_resize_size():
    # An XS estimate raised to S by the probe must still NOT fire the
    # detent — keyed on the RESIZED size, never the pre-resize tshirt (memo:
    # "key it on resized SIZE, not resolved ROUTE" — this asserts the size
    # half of that same requirement).
    decision = sa.route(
        appetite="large",
        estimate={"tshirt": "XS"},
        probe_signal="raise",
        premise_provenance="read",
    )
    assert decision["resolved_estimate"]["tshirt"] == "S"
    assert "premise_unproven" not in decision["detents"]

    decision2 = sa.route(
        appetite="large",
        estimate={"tshirt": "S"},
        probe_signal="raise",
        premise_provenance="read",
    )
    assert decision2["resolved_estimate"]["tshirt"] == "M"
    assert "premise_unproven" in decision2["detents"]


def test_premise_unproven_advisory_wording_present_in_next_move():
    decision = sa.route(
        appetite="large", estimate={"tshirt": "L"}, premise_provenance="read"
    )
    assert "premise_unproven" in decision["detents"]
    assert "ADVISORY" in decision["next_move"]
    assert "never block" in decision["next_move"]
    assert "does not alter the route" in decision["next_move"]


def test_premise_not_applicable_advisory_wording_present_in_next_move():
    decision = sa.route(
        appetite="large", estimate={"tshirt": "L"}, premise_provenance="not-applicable"
    )
    assert "premise_not_applicable" in decision["detents"]
    assert "ADVISORY" in decision["next_move"]
    assert "never block" in decision["next_move"]


def test_premise_provenance_never_populates_xl_exit_or_alters_route():
    baseline = sa.route(appetite="large", estimate={"tshirt": "XL"})
    read_decision = sa.route(
        appetite="large", estimate={"tshirt": "XL"}, premise_provenance="read"
    )
    assert read_decision["route"] == baseline["route"]
    assert read_decision["xl_exit"] is None
    assert read_decision["fork"] is None


def test_unknown_premise_provenance_raises_usage_error():
    with pytest.raises(sa.SizingAssembleError):
        sa.route(appetite="medium", estimate={"tshirt": "M"}, premise_provenance="bogus")


def test_unknown_premise_provenance_raises_even_on_express_lane_path():
    # _validate_premise_provenance fires regardless of whether express_lane
    # will end up consuming the value — same unconditional-validation
    # property as _validate_probe_signal (Finding 5).
    with pytest.raises(sa.SizingAssembleError):
        sa.route(
            appetite="small",
            estimate={"tshirt": "XL"},
            express_lane=True,
            premise_provenance="bogus",
        )


def test_premise_unproven_not_populated_on_express_lane_regardless_of_provenance():
    decision = sa.route(
        appetite="small",
        estimate={"tshirt": "XL"},
        express_lane=True,
        premise_provenance="read",
    )
    assert decision["detents"] == []


def test_detent_enum_widened_with_both_premise_values_together():
    assert "premise_unproven" in sa.DETENT_ENUM
    assert "premise_not_applicable" in sa.DETENT_ENUM


def test_cli_main_premise_provenance_flag(capsys):
    exit_code = sa.main(
        ["--appetite", "large", "--tshirt", "L", "--premise-provenance", "read"]
    )
    assert exit_code == sa.EXIT_OK
    out = capsys.readouterr().out
    assert '"premise_unproven"' in out


def test_cli_main_unknown_premise_provenance_usage_error(capsys):
    exit_code = sa.main(
        ["--appetite", "medium", "--tshirt", "M", "--premise-provenance", "bogus"]
    )
    assert exit_code == sa.EXIT_USAGE


# --- Provenance-invariance property test (memo: point-checks insufficient) --

_TSHIRTS = sa.TSHIRT_ORDER
_APPETITES = sa.APPETITE_ENUM
_PROBE_SIGNALS = (None, "collapse", "raise")
_BOOLS = (False, True)

_INVARIANCE_CASES = list(
    itertools.product(
        _TSHIRTS,
        _APPETITES,
        _PROBE_SIGNALS,
        _BOOLS,  # jtbd_unclear
        _BOOLS,  # well_trodden_step_change
        _BOOLS,  # express_lane
    )
)


def test_invariance_cases_count_rises_360_to_432_with_xxl_notch():
    # AC10: _INVARIANCE_CASES derives from sa.TSHIRT_ORDER, so it
    # auto-expands the moment a notch is appended/removed. Assert the count
    # explicitly (6 tshirts * 3 appetites * 3 probe signals * 8 bool combos
    # = 432, up from 360 at 5 tshirts) so a future ladder contraction is
    # loud rather than silent.
    assert len(sa.TSHIRT_ORDER) == 6
    assert len(_INVARIANCE_CASES) == 432


@pytest.mark.parametrize(
    "tshirt,appetite,probe_signal,jtbd_unclear,well_trodden_step_change,express_lane",
    _INVARIANCE_CASES,
)
def test_provenance_invariance_read_vs_executed(
    tshirt, appetite, probe_signal, jtbd_unclear, well_trodden_step_change, express_lane
):
    # A `read` call must produce IDENTICAL route()/fork/xl_exit/
    # resolved_estimate/scout_evidence/narration output to an `executed`
    # call — the only permitted divergence is `detents`/`next_move`. This
    # is what catches the shape-gate disjunction and the combined
    # appetite_exceeded + pm-decision branch across every routing shape,
    # and it survives a future seventh route (memo: point-checks
    # insufficient).
    kwargs = dict(
        appetite=appetite,
        estimate={"tshirt": tshirt},
        probe_signal=probe_signal,
        jtbd_unclear=jtbd_unclear,
        well_trodden_step_change=well_trodden_step_change,
        express_lane=express_lane,
    )
    read_decision = sa.route(**kwargs, premise_provenance="read")
    executed_decision = sa.route(**kwargs, premise_provenance="executed")
    for key in read_decision:
        if key in ("detents", "next_move"):
            continue
        assert read_decision[key] == executed_decision[key], key


@pytest.mark.parametrize(
    "tshirt,appetite,probe_signal,jtbd_unclear,well_trodden_step_change,express_lane",
    _INVARIANCE_CASES,
)
def test_provenance_invariance_not_applicable_vs_executed(
    tshirt, appetite, probe_signal, jtbd_unclear, well_trodden_step_change, express_lane
):
    kwargs = dict(
        appetite=appetite,
        estimate={"tshirt": tshirt},
        probe_signal=probe_signal,
        jtbd_unclear=jtbd_unclear,
        well_trodden_step_change=well_trodden_step_change,
        express_lane=express_lane,
    )
    na_decision = sa.route(**kwargs, premise_provenance="not-applicable")
    executed_decision = sa.route(**kwargs, premise_provenance="executed")
    for key in na_decision:
        if key in ("detents", "next_move"):
            continue
        assert na_decision[key] == executed_decision[key], key


# --- C2: absent-appetite path, M+/XS-S split, route-invariance (2026-08-07) -
#
# Additive only — see the plan's C2 body. Does not modify any test above;
# `test_appetite_exceeded_*` staying green unmodified is AC9's proof that the
# volunteered-appetite path is preserved, not replaced.


@pytest.mark.parametrize("tshirt", sa.TSHIRT_ORDER)
def test_absent_appetite_succeeds_and_omits_appetite_detents(tshirt):
    # AC1, AC3.
    decision = sa.route(estimate={"tshirt": tshirt})
    assert decision["route"] in sa.ROUTE_ENUM
    assert "appetite_conform" not in decision["detents"]
    assert "appetite_exceeded" not in decision["detents"]


@pytest.mark.parametrize("tshirt", ["M", "L", "XL"])
def test_absent_appetite_sets_post_size_prompt_pending_at_m_and_above(tshirt):
    # AC4.
    decision = sa.route(estimate={"tshirt": tshirt})
    assert "post_size_prompt_pending" in decision["detents"]


@pytest.mark.parametrize("tshirt", ["XS", "S"])
def test_absent_appetite_omits_post_size_prompt_pending_below_m(tshirt):
    # AC5: no post_size_prompt_pending, and next_move equals today's text for
    # that route (i.e. the same next_move a volunteered "small" appetite
    # would produce for the same tshirt, since neither appetite_conform nor
    # appetite_exceeded nor post_size_prompt_pending applies at XS/S).
    absent_decision = sa.route(estimate={"tshirt": tshirt})
    assert "post_size_prompt_pending" not in absent_decision["detents"]

    small_decision = sa.route(appetite="small", estimate={"tshirt": tshirt})
    assert absent_decision["next_move"] == small_decision["next_move"]


def test_absent_appetite_next_move_excludes_closed_fork_tokens():
    # AC6: the open question never contains the closed cut_to_fit/
    # raise_appetite pair.
    decision = sa.route(estimate={"tshirt": "M"})
    assert "post_size_prompt_pending" in decision["detents"]
    assert "cut_to_fit" not in decision["next_move"]
    assert "raise_appetite" not in decision["next_move"]


def test_absent_appetite_next_move_bundles_open_question_at_pm_decision():
    # AC6a, re-cut: the appetite plan specced SUPPRESSION here, on the premise
    # that the pm-decision branch offered `split` as one of its enumerated
    # exits. The XXL plan landed second and dropped `split` from that text,
    # leaving XL — the size where "want to split it?" is the likeliest PM
    # answer — as the one size whose next_move carried no open question at
    # all. The two now arrive BUNDLED as one ask, mirroring the
    # appetite_exceeded + pm-decision combined arm.
    decision = sa.route(estimate={"tshirt": "XL"})
    assert decision["route"] == "pm-decision"
    assert "post_size_prompt_pending" in decision["detents"]
    assert "shall we go with that or want to split it" in decision["next_move"]
    # AC6a's live invariant: exactly one PM-facing question in next_move.
    assert decision["next_move"].count("?") == 1
    # AC6: the appetite-absent path never offers the closed pair.
    assert "cut_to_fit" not in decision["next_move"]
    assert "raise_appetite" not in decision["next_move"]


def test_pm_decision_enumerated_exits_still_omit_split():
    # The XXL plan's C2 item 4 ruling stands — `split` is a vocabulary value
    # in XL_EXIT_ENUM, not a menu item in the enumerated exits. The bundled
    # open question above reintroduces splitting as an OPEN answer, which is
    # a different thing; this pins that the menu itself did not grow back.
    decision = sa.route(estimate={"tshirt": "XL"})
    exits_text = decision["next_move"].split("Put that to the PM")[0]
    assert "split (" not in exits_text


def test_absent_appetite_question_precedes_advisory_at_pm_decision():
    # The ordering rule holds on the pm-decision path too: a live PM question
    # must never sit behind a non-actionable advisory.
    decision = sa.route(estimate={"tshirt": "XL"}, premise_provenance="not-applicable")
    assert "post_size_prompt_pending" in decision["detents"]
    assert "premise_not_applicable" in decision["detents"]
    assert decision["next_move"].index("shall we go with that") < decision[
        "next_move"
    ].index("ADVISORY")


def test_absent_appetite_next_move_question_precedes_premise_advisory():
    # AC6: ordering — when both the open question and a premise advisory
    # apply, the question comes first.
    decision = sa.route(
        estimate={"tshirt": "L"}, premise_provenance="read"
    )
    assert "post_size_prompt_pending" in decision["detents"]
    assert "premise_unproven" in decision["detents"]
    question_idx = decision["next_move"].index("shall we go with that")
    advisory_idx = decision["next_move"].index("ADVISORY")
    assert question_idx < advisory_idx


def test_absent_appetite_narration_omits_appetite_clause_and_never_says_none():
    # AC7.
    decision = sa.route(estimate={"tshirt": "M"})
    assert "appetite=None" not in decision["narration"]
    assert "against appetite=" not in decision["narration"]


@pytest.mark.parametrize("tshirt", sa.TSHIRT_ORDER)
def test_route_invariant_across_appetite_presence(tshirt):
    # AC2 — the invariance test DoE explicitly asked for: for every t-shirt,
    # `route` is identical across appetite absent / small / medium / large.
    routes = {
        sa.route(estimate={"tshirt": tshirt})["route"],
        sa.route(appetite="small", estimate={"tshirt": tshirt})["route"],
        sa.route(appetite="medium", estimate={"tshirt": tshirt})["route"],
        sa.route(appetite="large", estimate={"tshirt": tshirt})["route"],
    }
    assert len(routes) == 1


def test_absent_appetite_still_validates_present_appetite_value():
    # The optionality must not weaken validation of a present value.
    with pytest.raises(sa.SizingAssembleError):
        sa.route(appetite="huge", estimate={"tshirt": "M"})


def test_post_size_prompt_pending_in_detent_enum():
    assert "post_size_prompt_pending" in sa.DETENT_ENUM


# --- AC7/AC13/AC14: vendored schema widen (2026-08-07 sizing-ladder-xxl-
# notch-and-goal-setting-route plan, C4) -----------------------------------


def _vendored_sizing_object_fixture():
    return {
        "schema": "sizing-object",
        "intent": "Ship the sixth notch.",
        "estimate": {"tshirt": "XXL", "provisional": True},
        "route": "goal-setting",
        "detents": ["xxl_unprobed", "goal_setting_pm_gated"],
        "fork": None,
        "xl_exit": None,
        "status": "routed",
        "premise": {"provenance": "unrecorded"},
    }


def test_vendored_sizing_object_schema_validates_full_xxl_object():
    from coordinator_core.frontmatter.schema_validate import validate

    result = validate("sizing-object", _vendored_sizing_object_fixture())
    assert result["ok"], result.get("errors")


def test_vendored_sizing_object_schema_still_rejects_unknown_tshirt():
    from coordinator_core.frontmatter.schema_validate import validate

    fixture = _vendored_sizing_object_fixture()
    fixture["estimate"] = {"tshirt": "XXXL", "provisional": True}
    result = validate("sizing-object", fixture)
    assert not result["ok"]


def test_vendored_completion_entry_schema_validates_xxl_loe_tshirt():
    from coordinator_core.frontmatter.schema_validate import validate

    result = validate(
        "completion-entry",
        {
            "title": "Sixth-notch smoke",
            "created": "2026-08-07",
            "nature": "infra",
            "loe": {"tshirt": "XXL"},
        },
    )
    assert result["ok"], result.get("errors")


def test_write_guard_validate_frontmatter_schema_deny_accepts_real_xxl_sizing_object(
    tmp_path, monkeypatch
):
    # AC7 write-guard leg -- the 2026-08-06 vendoring inversion moved the
    # duty to validate against the claude-klabauter-side write path
    # (validate_frontmatter_schema_deny), not just the validator leg above.
    pytest.importorskip("yaml")
    from coordinator_core.testing.doe_root import doe_root_and_present
    from coordinator_core.write_guards import validate_frontmatter_schema_deny as guard

    doe_root, doe_present = doe_root_and_present()
    if not doe_present:
        pytest.skip("sibling DoE-claude checkout not found")
    monkeypatch.setattr(guard, "coordinator_doe_root", lambda: doe_root)

    import yaml

    content = "---\n" + yaml.safe_dump(_vendored_sizing_object_fixture()) + "---\n"
    rel = "state/sizings/2026-08-07-c5-xxl-write-guard-smoke.yaml"
    file_path = tmp_path / rel
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    result = guard.check(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(file_path), "content": content},
            "cwd": str(tmp_path),
        }
    )
    if result is not None:
        hso = result.get("hookSpecificOutput", {})
        assert hso.get("permissionDecision") != "deny", hso.get("permissionDecisionReason")


def test_sizing_object_schema_version_and_bump_class():
    # AC13.
    import json
    import pathlib

    schema = json.loads(
        pathlib.Path(
            "coordinator_core/frontmatter/schemas/sizing-object.schema.json"
        ).read_text(encoding="utf-8")
    )
    # 1.18.0. Re-vendored 2026-08-25 from DoE `42cb0db61` byte-identically;
    # `declined_note` is the sole structural delta, mirroring `superseded_note`
    # so a `declined` record's mandated decision-record backlink has a joinable
    # home instead of a YAML comment.
    #
    # THIS PIN HAD SILENTLY DRIFTED THREE MINORS, and that is the finding worth
    # keeping. It read 1.14.0 while the vendored copy was already 1.17.0
    # (`397d0dd32` adopted `peer_notes` at 1.16.0 and optional `name` at 1.17.0),
    # so this assertion was ALREADY RED before the 1.18.0 re-vendor — the number
    # moved, the redness did not start here. A pin naming a version rather than
    # deriving it goes stale the moment someone re-vendors without reading this
    # file, and nothing else in the tier says so.
    #
    # NOT converted to `== json.load(doe_source)["x-schema-version"]`, which
    # would never go stale: that makes this test unable to fail on an unreviewed
    # re-vendor, which is the thing it exists to catch. The staleness is the
    # cost of the check, not a defect in it. Move the number deliberately when
    # you re-vendor, and say what the bump added.
    #
    # CORRECTION to the note this replaces, which said to hold a bump because
    # an unequal version otherwise goes silent: it does not. DoE's
    # `test_vendored_schema_matches_doe_source` asserts version equality HARD
    # (coordinator/tests/test_vendored_schema_version_parity.py, the
    # doe_version == claude_klabauter_version assert) and only reaches the shape-hash
    # compare once that passes; its only skips are an unresolvable sibling and
    # an unvendored schema. BOTH stagger directions are red, not one red and
    # one quiet — and it reads our COMMITTED HEAD, so their red clears when we
    # commit, not when we write. Do not reintroduce the hold-for-silence
    # reasoning.
    # Moved 1.18.0 -> 1.20.0 (2026-09-05) after reading what the two bumps
    # added, per this note's own instruction. Both are `nested-field-additive`
    # and the schema states them itself in `x-bump-note`: 1.19.0 adds optional
    # `em_analysis` (topic-keyed EM analysis, closing the bug-backlog row about
    # analysis degrading to unqueryable prose); 1.20.0 adds optional
    # `blocked_by` and `awaiting_gate`, the two fields the engine's own sizing
    # narration already told EMs to write. Nothing joined `required` and no
    # existing property changed shape.
    #
    # The pin had gone red the way the note above predicts: the schema was
    # re-vendored at 6c19ce193e (2026-08-30) and this number was not read.
    assert schema["x-schema-version"] == "1.20.0"
    # NEGATIVE SPEC: `x-bump-class` is asserted ABSENT, not equal to
    # `nested-field-additive` — and absent is the PERMANENT answer for this
    # schema, not a waiting state. DoE's `9f4c0c17b` (2026-08-10, "schemas: drop
    # the bump-class the 1.10.0 label reconciliation never earned") removed the
    # key deliberately, replacing it with a `$comment`: that bump was
    # label-reconciliation-only with zero shape effect, so it earned no class.
    # Read at their own commits, NEITHER 1.12.0 (`f8c4b15b2`) NOR 1.13.0
    # (`86d786147`) carries the key; only `x-bump-note` survives. Correcting the
    # note this replaces, which said 1.13.0 carried it — it did not, and any
    # local copy that appears to is a pre-`9f4c0c17b` vendoring.
    #
    # Do NOT hand-restore the key here: that would manufacture drift against a
    # deliberate decision, and DoE's `test_vendored_schema_matches_doe_source`
    # hashes shape. And do NOT read a red here as a re-vendor signal — restoring
    # the key would mean reverting `9f4c0c17b`, which doe-claude-em has stated
    # they will not do (memo
    # 2026-08-13-doe-claude-em-bump-class-deliberately-absent.md).
    assert "x-bump-class" not in schema


def test_completion_entry_schema_version_and_bump_class():
    # AC13.
    import json
    import pathlib

    schema = json.loads(
        pathlib.Path(
            "coordinator_core/frontmatter/schemas/completion-entry.schema.json"
        ).read_text(encoding="utf-8")
    )
    # 1.4.0 since 62b27af07 re-vendored this mirror for the XXL tier (leg 3 of
    # a three-leg coordinated landing). The bump class is unchanged; only the
    # version pin here was left behind.
    assert schema["x-schema-version"] == "1.4.0"
    assert schema["x-bump-class"] == "enum-value-additive"


def test_vendored_schema_widened_enums_order_exact():
    # AC14 -- order-exact against the agreed emitted arrays (C4's body),
    # append-at-end, no re-sorting. Not merely set-equal.
    import json
    import pathlib

    sizing_schema = json.loads(
        pathlib.Path(
            "coordinator_core/frontmatter/schemas/sizing-object.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert sizing_schema["properties"]["estimate"]["properties"]["tshirt"]["enum"] == [
        "XS",
        "S",
        "M",
        "L",
        "XL",
        "XXL",
    ]
    assert sizing_schema["properties"]["route"]["enum"] == [
        "dispatch",
        "spec-dispatch",
        "shape",
        "plan",
        "roadmap",
        "pm-decision",
        "goal-setting",
    ]
    assert sizing_schema["properties"]["detents"]["items"]["enum"] == [
        "appetite_conform",
        "appetite_exceeded",
        "route_boundary_crossed",
        "scope_boundary_acknowledged",
        "pm_decision_pending",
        "premise_unproven",
        "premise_not_applicable",
        "post_size_prompt_pending",
        "xxl_unprobed",
        "goal_setting_pm_gated",
        # Appended by the sizing-guard-flags widen (1.9.0). Order-exact and
        # append-at-end: enum ORDER is load-bearing against DoE's
        # EQUAL_VERSION_SHAPE_DRIFT gate, and these are the exact bytes their
        # side stamps 1.9.0 against.
        "boundary_counted_in_notch",
        "scout_evidence_mention_count",
        # Appended by the routine-ask-sized-XL widen (2026-08-11). Same
        # order-exact, append-at-end discipline as the two above — DoE's
        # canonical copy needs these three in exactly this position before
        # their parity gate goes green (memo sent same day).
        "intent_em_elaborated",
        "precedent_shipped_before",
        "probe_raise_on_substrate_condition",
        # 1.12.0: doe-claude-em's counter — the symmetric mark on ask-scope,
        # so the notch-preserving answer is no longer the unmarked one.
        "probe_raise_ask_scope_asserted",
        # 1.13.0: the breadth arm (cross-repo memo 2026-08-12-doe-claude-em-
        # sizing-breadth-arm.md, adopted). Same order-exact, append-at-end
        # discipline as every widen above — never re-sort.
        "probe_raise_on_breadth",
    ]

    completion_schema = json.loads(
        pathlib.Path(
            "coordinator_core/frontmatter/schemas/completion-entry.schema.json"
        ).read_text(encoding="utf-8")
    )
    tshirt_arm = completion_schema["properties"]["loe"]["properties"]["tshirt"]["anyOf"][0]
    assert tshirt_arm["enum"] == ["XS", "S", "M", "L", "XL", "XXL"]


def test_vendored_schema_enums_stay_parity_with_the_engine_tuples():
    """The two tests directly above pin the vendored schema's `detents` and
    `route` enums against a HARDCODED literal, and this module's own tests
    never check them against `DETENT_ENUM`/`ROUTE_ENUM` at all — so a tuple
    widen that forgot to touch the vendored schema (or the schema half of a
    bump that forgot the tuple) could land and stay green, since neither
    side's suite reads the other's source of truth. This test closes that
    gap directly: engine tuple and vendored schema enum, compared ORDER-EXACT
    (not set-equal — order is load-bearing against DoE's
    EQUAL_VERSION_SHAPE_DRIFT gate, same as the hardcoded-literal tests
    above)."""
    import json
    import pathlib

    sizing_schema = json.loads(
        pathlib.Path(
            "coordinator_core/frontmatter/schemas/sizing-object.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert list(sa.DETENT_ENUM) == sizing_schema["properties"]["detents"]["items"]["enum"]
    assert list(sa.ROUTE_ENUM) == sizing_schema["properties"]["route"]["enum"]


# --- AC12: goal_setting_pm_gated detent ------------------------------------


def test_goal_setting_pm_gated_in_detent_enum():
    assert "goal_setting_pm_gated" in sa.DETENT_ENUM


def test_goal_setting_pm_gated_fires_when_route_is_goal_setting():
    decision = sa.route(estimate={"tshirt": "XXL"})
    assert decision["route"] == "goal-setting"
    assert "goal_setting_pm_gated" in decision["detents"]


def test_goal_setting_pm_gated_never_fires_off_goal_setting_route():
    decision = sa.route(estimate={"tshirt": "M"})
    assert decision["route"] != "goal-setting"
    assert "goal_setting_pm_gated" not in decision["detents"]


def test_post_size_prompt_tshirts_covers_every_tshirt_order_notch_from_m_up():
    # AC17 — forward-compat property test, derived from TSHIRT_ORDER itself
    # rather than hard-coded to today's XS/S/M/L/XL. Reds the moment a
    # future notch (e.g. XXL) is appended above M without a matching update
    # to _POST_SIZE_PROMPT_TSHIRTS.
    m_index = sa.TSHIRT_ORDER.index("M")
    for tshirt in sa.TSHIRT_ORDER[m_index:]:
        assert tshirt in sa._POST_SIZE_PROMPT_TSHIRTS


# --- Sizing-lobby guards become required flags (cross-repo memo
# --- 2026-08-10-doe-claude-em-sizing-guard-flags.md) ------------------------
#
# Both flags replay the --premise-provenance shape: a typed answer that
# reaches the validator, an advisory detent, a next_move advisory, and no
# effect on route/xl_exit. The premise block above is the parity reference —
# where a property holds for both, it is asserted for both.


def test_boundary_and_scout_kind_detents_in_detent_enum():
    assert "boundary_counted_in_notch" in sa.DETENT_ENUM
    assert "scout_evidence_mention_count" in sa.DETENT_ENUM


def test_boundary_in_notch_yes_fires_detent():
    decision = sa.route(estimate={"tshirt": "S"}, boundary_in_notch="yes")
    assert "boundary_counted_in_notch" in decision["detents"]


def test_boundary_in_notch_no_and_absent_fire_nothing():
    for value in ("no", None):
        decision = sa.route(estimate={"tshirt": "S"}, boundary_in_notch=value)
        assert "boundary_counted_in_notch" not in decision["detents"], value


@pytest.mark.parametrize("tshirt", ["XS", "S", "M", "L", "XL", "XXL"])
def test_boundary_in_notch_is_not_size_gated(tshirt):
    # The deliberate asymmetry against _PREMISE_DETENT_TSHIRTS. The motivating
    # incident was an S sized XL: a detent gated at M-and-above would exempt
    # it the moment the collapse it is meant to prompt actually landed.
    decision = sa.route(estimate={"tshirt": tshirt}, boundary_in_notch="yes")
    assert "boundary_counted_in_notch" in decision["detents"], tshirt


def test_boundary_advisory_names_the_memo_vs_co_design_discriminator():
    # The engine cannot apply the discriminator (the flag has two values, not
    # three), so the advisory must HAND it over rather than render a verdict.
    decision = sa.route(estimate={"tshirt": "L"}, boundary_in_notch="yes")
    text = decision["next_move"]
    assert "ADVISORY (warn, never block; does not alter the route above)" in text
    assert "co-design" in text
    assert "GATE, not a size" in text


def test_scout_evidence_kind_mention_count_fires_detent():
    decision = sa.route(estimate={"tshirt": "M"}, scout_evidence_kind="mention-count")
    assert "scout_evidence_mention_count" in decision["detents"]


@pytest.mark.parametrize("kind", ["change-set", "site-count", None])
def test_scout_evidence_kind_qualified_answers_fire_nothing(kind):
    decision = sa.route(estimate={"tshirt": "M"}, scout_evidence_kind=kind)
    assert "scout_evidence_mention_count" not in decision["detents"]


def test_scout_evidence_kind_advisory_names_the_compat_shim():
    decision = sa.route(estimate={"tshirt": "M"}, scout_evidence_kind="mention-count")
    text = decision["next_move"]
    assert "ADVISORY (warn, never block; does not alter the route above)" in text
    assert "MENTION-COUNT" in text
    assert "back-compat shim" in text


def test_scout_evidence_kind_is_independent_of_scout_evidence_contents():
    # The negative-spec leg: the KIND is never inferred from the list. A long
    # list with kind=change-set stays silent; an EMPTY list with
    # kind=mention-count still fires. Neither length nor contents is read.
    long_list = [f"file_{n}.py mentions the literal" for n in range(108)]
    quiet = sa.route(
        estimate={"tshirt": "M"}, scout_evidence=long_list, scout_evidence_kind="change-set"
    )
    assert "scout_evidence_mention_count" not in quiet["detents"]

    loud = sa.route(
        estimate={"tshirt": "M"}, scout_evidence=[], scout_evidence_kind="mention-count"
    )
    assert "scout_evidence_mention_count" in loud["detents"]

    # And the echo-back stays byte-identical — passthrough, never rewritten.
    assert quiet["scout_evidence"] == long_list


@pytest.mark.parametrize("tshirt", ["XS", "S", "M", "L", "XL", "XXL"])
def test_neither_new_detent_alters_route_or_xl_exit(tshirt):
    baseline = sa.route(estimate={"tshirt": tshirt})
    flagged = sa.route(
        estimate={"tshirt": tshirt},
        boundary_in_notch="yes",
        scout_evidence_kind="mention-count",
    )
    assert flagged["route"] == baseline["route"], tshirt
    assert flagged["xl_exit"] is None, tshirt
    assert baseline["xl_exit"] is None, tshirt
    assert flagged["resolved_estimate"] == baseline["resolved_estimate"], tshirt


def test_unknown_boundary_in_notch_raises_usage_error():
    with pytest.raises(sa.SizingAssembleError):
        sa.route(estimate={"tshirt": "M"}, boundary_in_notch="maybe")


def test_unknown_scout_evidence_kind_raises_usage_error():
    with pytest.raises(sa.SizingAssembleError):
        sa.route(estimate={"tshirt": "M"}, scout_evidence_kind="grep-count")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"boundary_in_notch": "maybe"},
        {"scout_evidence_kind": "grep-count"},
    ],
)
def test_new_validators_fire_even_on_express_lane_path(kwargs):
    # Same unconditional-validation property as _validate_probe_signal /
    # _validate_premise_provenance (Finding 5): validation runs BEFORE the
    # express_lane short-circuit, not only on paths reaching detent
    # computation.
    with pytest.raises(sa.SizingAssembleError):
        sa.route(estimate={"tshirt": "M"}, express_lane=True, **kwargs)


def test_new_detents_never_populated_on_express_lane():
    decision = sa.route(
        estimate={"tshirt": "L"},
        express_lane=True,
        boundary_in_notch="yes",
        scout_evidence_kind="mention-count",
    )
    assert decision["detents"] == []
    assert decision["route"] == "dispatch"


def test_new_advisories_follow_the_premise_advisory_in_next_move():
    # Append-not-reorder discipline: the ordering contract is route text ->
    # appetite question -> xxl_unprobed -> premise -> boundary -> scout-kind.
    decision = sa.route(
        estimate={"tshirt": "L"},
        premise_provenance="read",
        boundary_in_notch="yes",
        scout_evidence_kind="mention-count",
    )
    text = decision["next_move"]
    premise_at = text.index("mechanism premise was READ")
    boundary_at = text.index("contributed to this notch")
    scout_at = text.index("MENTION-COUNT")
    assert premise_at < boundary_at < scout_at


def test_cli_main_boundary_and_scout_kind_flags(capsys):
    rc = sa.main(
        [
            "--tshirt",
            "L",
            "--boundary-in-notch",
            "yes",
            "--scout-evidence-kind",
            "mention-count",
        ]
    )
    out = capsys.readouterr().out
    assert rc == sa.EXIT_OK
    assert '"boundary_counted_in_notch"' in out
    assert '"scout_evidence_mention_count"' in out


@pytest.mark.parametrize(
    "argv",
    [
        ["--tshirt", "M", "--boundary-in-notch", "maybe"],
        ["--tshirt", "M", "--scout-evidence-kind", "grep-count"],
    ],
)
def test_cli_main_unknown_new_flag_values_usage_error(argv, capsys):
    rc = sa.main(argv)
    capsys.readouterr()
    assert rc == sa.EXIT_USAGE


def test_usage_string_advertises_both_new_flags(capsys):
    sa.main([])
    err = capsys.readouterr().err
    assert "--boundary-in-notch yes|no" in err
    assert "--scout-evidence-kind mention-count|change-set|site-count" in err


def test_vendored_schema_validates_object_carrying_both_new_detents():
    from coordinator_core.frontmatter.schema_validate import validate

    fixture = _vendored_sizing_object_fixture()
    fixture["detents"] = ["boundary_counted_in_notch", "scout_evidence_mention_count"]
    result = validate("sizing-object", fixture)
    assert result["ok"], result.get("errors")


# --- C6: mechanical two-sense guard over the pejorative "appetite" sense ----
#
# AC13a (test-backed). AC13b (the git-diff scope-subset assertion) is
# deliberately NOT shipped as a test — see the plan's C6 body: it is vacuous
# by construction once this work lands (a post-landing `git diff --name-only`
# always returns the empty set, trivially a subset of scope forever). It is
# an executor-run verification step reported in the run-report, not a test.


def test_pejorative_appetite_sense_preserved_in_phantom_sweep_test():
    import pathlib

    text = pathlib.Path(
        "coordinator_core/ceremony_common/test_phantom_resolves_id_sweep.py"
    ).read_text(encoding="utf-8")
    assert "concrete, non-appetite reason" in text


def test_pejorative_appetite_sense_preserved_in_plan_coverage_check_template():
    import pathlib

    text = pathlib.Path(
        "coordinator_core/ops/docgen/templates/plan_coverage_check.json"
    ).read_text(encoding="utf-8")
    assert "### Weak OOS / hedges (appetite-based deferrals)" in text


def test_pejorative_appetite_sense_preserved_in_coordinator_doc_new():
    import pathlib

    text = pathlib.Path("coordinator/bin/coordinator-doc-new.py").read_text(
        encoding="utf-8"
    )
    assert "### Weak OOS / hedges (appetite-based deferrals)" in text


# ---------------------------------------------------------------------------
# Routine-ask-sized-XL guards (2026-08-11 incident).
#
# The PM asked "commit it and push it" — an operation this fleet had shipped
# repeatedly — and the lobby returned XL/pm-decision. Three inputs were
# missing: the engine never saw the ask, nothing recorded that the operation
# had precedent, and a probe raise built on the SUBSTRATE's condition was
# indistinguishable from one built on the ASK's scope.
# ---------------------------------------------------------------------------


def test_substrate_condition_raise_is_not_applied():
    """The incident's exact mechanism: a raise justified by findings about the
    area, not the ask, must not move the notch."""
    out = sa.route(
        estimate={"tshirt": "L"},
        probe_signal="raise",
        probe_raise_basis="substrate-condition",
    )
    assert out["resolved_estimate"]["tshirt"] == "L"
    assert "probe_raise_on_substrate_condition" in out["detents"]
    # The whole point: L routes to plan, XL would have routed to pm-decision.
    assert out["route"] == "plan"
    assert "route_boundary_crossed" not in out["detents"]


def test_ask_scope_raise_still_applies():
    """The suppression must not break the legitimate raise — an ask that
    genuinely grew still moves the notch (Finding 2's under-read net)."""
    out = sa.route(
        estimate={"tshirt": "L"},
        probe_signal="raise",
        probe_raise_basis="ask-scope",
    )
    assert out["resolved_estimate"]["tshirt"] == "XL"
    assert "probe_raise_on_substrate_condition" not in out["detents"]
    assert out["route"] == "pm-decision"


def test_raise_without_a_declared_basis_is_unchanged():
    """Back-compat: every existing caller omits the flag and must be unaffected."""
    out = sa.route(estimate={"tshirt": "L"}, probe_signal="raise")
    assert out["resolved_estimate"]["tshirt"] == "XL"
    assert "probe_raise_on_substrate_condition" not in out["detents"]


def test_collapse_is_never_suppressed_by_the_basis_flag():
    """The basis flag qualifies a RAISE only — a collapse is the direction with
    a downstream net and must always land."""
    out = sa.route(
        estimate={"tshirt": "L"},
        probe_signal="collapse",
        probe_raise_basis="substrate-condition",
    )
    assert out["resolved_estimate"]["tshirt"] == "M"
    assert "probe_raise_on_substrate_condition" not in out["detents"]


def test_intent_is_echoed_so_the_ask_and_the_size_share_a_frame():
    out = sa.route(
        estimate={"tshirt": "XL"}, intent="commit it and push it"
    )
    assert out["intent"] == "commit it and push it"


def test_intent_echoed_on_the_express_lane_too():
    out = sa.route(
        estimate={"tshirt": "XS"}, intent="commit it", express_lane=True
    )
    assert out["intent"] == "commit it"
    assert out["detents"] == []


def test_em_elaborated_intent_fires_at_every_size():
    """Not size-gated, for boundary_counted_in_notch's reason: the size is what
    an elaborated intent is suspected of having inflated."""
    for tshirt in ("XS", "S", "M", "L", "XL", "XXL"):
        out = sa.route(
            estimate={"tshirt": tshirt}, intent_source="em-elaborated"
        )
        assert "intent_em_elaborated" in out["detents"], tshirt
        assert "verbatim" in out["next_move"]


def test_pm_verbatim_intent_fires_nothing():
    out = sa.route(
        estimate={"tshirt": "XL"}, intent_source="pm-verbatim"
    )
    assert "intent_em_elaborated" not in out["detents"]


def test_shipped_before_precedent_fires_and_names_the_discharge():
    out = sa.route(
        estimate={"tshirt": "L"}, precedent="shipped-before"
    )
    assert "precedent_shipped_before" in out["detents"]
    assert "SHIPPED IN THIS REPO BEFORE" in out["next_move"]
    # Advisory only — never touches the route (module negative-spec).
    assert out["route"] == "plan"


def test_novel_precedent_fires_nothing():
    out = sa.route(estimate={"tshirt": "L"}, precedent="novel")
    assert "precedent_shipped_before" not in out["detents"]


def test_the_incident_replayed_end_to_end():
    """The exact call made on 2026-08-11, plus the three new answers, must no
    longer produce an XL."""
    out = sa.route(
        estimate={"tshirt": "L"},
        premise_provenance="executed",
        probe_signal="raise",
        boundary_in_notch="no",
        scout_evidence_kind="change-set",
        scout_evidence=["378 dirty files in the klabauter mirror"],
        intent="commit it and push it",
        intent_source="em-elaborated",
        precedent="shipped-before",
        probe_raise_basis="substrate-condition",
    )
    assert out["resolved_estimate"]["tshirt"] == "L"
    assert out["route"] != "pm-decision"
    assert "pm_decision_pending" not in out["detents"]
    for expected in (
        "intent_em_elaborated",
        "precedent_shipped_before",
        "probe_raise_on_substrate_condition",
    ):
        assert expected in out["detents"]


def test_new_flags_validate_unconditionally_even_on_express_lane():
    """Same unconditional-validation property every sibling validator has
    (Finding 5, code-reviewer 2026-07-24)."""
    for kwargs in (
        {"intent_source": "bogus"},
        {"precedent": "bogus"},
        {"probe_raise_basis": "bogus"},
    ):
        with pytest.raises(sa.SizingAssembleError):
            sa.route(
                estimate={"tshirt": "XS"}, express_lane=True, **kwargs
            )


def test_new_detents_are_declared_in_the_enum():
    for name in (
        "intent_em_elaborated",
        "precedent_shipped_before",
        "probe_raise_on_substrate_condition",
    ):
        assert name in sa.DETENT_ENUM


# ---------------------------------------------------------------------------
# The ask-scope symmetric mark (doe-claude-em's counter, 1.12.0).
#
# Without it, `substrate-condition` cost the EM the raise while `ask-scope`
# cost nothing and was recorded nowhere queryable — an honesty gradient where
# the notch-preserving answer was the free, unmarked one.
# ---------------------------------------------------------------------------


def test_ask_scope_raise_leaves_a_queryable_mark():
    out = sa.route(
        estimate={"tshirt": "L"}, probe_signal="raise", probe_raise_basis="ask-scope"
    )
    assert "probe_raise_ask_scope_asserted" in out["detents"]
    # Marked, but NOT penalised — the raise it names still applied.
    assert out["resolved_estimate"]["tshirt"] == "XL"
    assert "ASK-SCOPE" in out["next_move"]


def test_both_basis_answers_now_leave_a_mark():
    """The gradient is closed: neither answer is the silent one."""
    marks = set()
    for basis in ("ask-scope", "substrate-condition"):
        out = sa.route(
            estimate={"tshirt": "L"}, probe_signal="raise", probe_raise_basis=basis
        )
        found = [
            d
            for d in out["detents"]
            if d in (
                "probe_raise_ask_scope_asserted",
                "probe_raise_on_substrate_condition",
            )
        ]
        assert len(found) == 1, (basis, out["detents"])
        marks.add(found[0])
    assert len(marks) == 2, "each answer must leave its OWN distinct mark"


def test_ask_scope_mark_needs_an_actual_raise():
    """The detent describes a raise's basis — with no raise there is nothing to
    describe, so a stray flag must not manufacture one."""
    for signal in (None, "collapse"):
        out = sa.route(
            estimate={"tshirt": "L"},
            probe_signal=signal,
            probe_raise_basis="ask-scope",
        )
        assert "probe_raise_ask_scope_asserted" not in out["detents"], signal


def test_ask_scope_mark_is_advisory_only():
    """Same advisory contract as its siblings — never touches route/xl_exit."""
    marked = sa.route(
        estimate={"tshirt": "S"}, probe_signal="raise", probe_raise_basis="ask-scope"
    )
    unmarked = sa.route(estimate={"tshirt": "S"}, probe_signal="raise")
    assert marked["route"] == unmarked["route"]
    assert marked["xl_exit"] is None
    assert marked["resolved_estimate"] == unmarked["resolved_estimate"]


def test_ask_scope_detent_is_declared_in_the_enum():
    assert "probe_raise_ask_scope_asserted" in sa.DETENT_ENUM


# ---------------------------------------------------------------------------
# The breadth arm (cross-repo memo 2026-08-12-doe-claude-em-sizing-breadth-
# arm.md, adopted): a raise resting solely on a touchpoint COUNT is a
# dispatch shape, not a size signal, and must not move the notch — same
# suppression contract as `substrate-condition`, distinct detent.
# ---------------------------------------------------------------------------


def test_breadth_raise_is_not_applied():
    """A raise justified only by site count must not move the notch, and
    fires its own detent rather than the substrate-condition one."""
    out = sa.route(
        estimate={"tshirt": "L"},
        probe_signal="raise",
        probe_raise_basis="breadth",
    )
    assert out["resolved_estimate"]["tshirt"] == "L"
    assert "probe_raise_on_breadth" in out["detents"]
    assert "probe_raise_on_substrate_condition" not in out["detents"]
    # The whole point: L routes to plan, XL would have routed to pm-decision.
    assert out["route"] == "plan"
    assert "route_boundary_crossed" not in out["detents"]


def test_breadth_and_substrate_condition_detents_are_disjoint():
    """Each suppressing basis fires exactly its own detent, never the other's."""
    for basis, expected, other in (
        ("breadth", "probe_raise_on_breadth", "probe_raise_on_substrate_condition"),
        ("substrate-condition", "probe_raise_on_substrate_condition", "probe_raise_on_breadth"),
    ):
        out = sa.route(
            estimate={"tshirt": "L"}, probe_signal="raise", probe_raise_basis=basis
        )
        assert expected in out["detents"], basis
        assert other not in out["detents"], basis


def test_breadth_basis_validates():
    """`breadth` is accepted by `_validate_probe_raise_basis`; an unknown
    basis still raises, naming all three values."""
    sa._validate_probe_raise_basis("breadth")  # no raise
    with pytest.raises(sa.SizingAssembleError) as exc_info:
        sa._validate_probe_raise_basis("bogus")
    assert "ask-scope" in str(exc_info.value)
    assert "substrate-condition" in str(exc_info.value)
    assert "breadth" in str(exc_info.value)


def test_breadth_collapse_and_bare_raise_paths_unchanged():
    """`collapse` and a bare `raise` (no basis) are untouched by the breadth
    arm's addition."""
    collapsed = sa.route(
        estimate={"tshirt": "L"},
        probe_signal="collapse",
        probe_raise_basis="breadth",
    )
    assert collapsed["resolved_estimate"]["tshirt"] == "M"
    assert "probe_raise_on_breadth" not in collapsed["detents"]

    bare = sa.route(estimate={"tshirt": "L"}, probe_signal="raise")
    assert bare["resolved_estimate"]["tshirt"] == "XL"
    assert "probe_raise_on_breadth" not in bare["detents"]


def test_breadth_detent_is_declared_in_the_enum():
    assert "probe_raise_on_breadth" in sa.DETENT_ENUM


# ---------------------------------------------------------------------------
# 2026-09-05 route flight recorder (cross-repo memo
# 2026-09-05-doe-claude-em-sizing-route-flight-recorder-should-emit-itself):
# the lobby's stage rows are computed from route + resized tshirt rather than
# transcribed by the EM. The failure these cover is the one transcription has:
# a chain that stops one row short, or a terminal picked from the route instead
# of the size.
# ---------------------------------------------------------------------------


def test_every_route_in_the_enum_has_a_stage_chain():
    """Totality is the point. A route added to ROUTE_ENUM without a chain would
    emit a recorder that silently stops early, so the table asserts at import —
    this test is the statement of that contract, not a second implementation."""
    for route_name in sa.ROUTE_ENUM:
        chain = sa.stages(route_name, "M")
        assert chain["rows"], route_name


def test_terminal_is_a_function_of_size_not_route():
    """XS/S close at quick-wrap, M and above at /workstream-complete — the one
    rule most likely to be got wrong by hand, because the route is the salient
    field and the size is not."""
    assert sa.stages("plan", "S")["terminal"] == "quick-wrap"
    assert sa.stages("plan", "M")["terminal"] == "/workstream-complete"
    assert sa.stages("dispatch", "XS")["terminal"] == "quick-wrap"
    assert sa.stages("dispatch", "M")["terminal"] == "/workstream-complete"


def test_a_lobby_owned_chain_ends_on_its_terminal():
    chain = sa.stages("spec-dispatch", "S")
    assert chain["owned_by"] == "lobby"
    assert chain["rows"][-1] == chain["terminal"] == "quick-wrap"


def test_a_room_owned_route_records_an_entry_row_and_stops():
    """shape/roadmap/goal-setting/pm-decision: the room owns everything after
    the entry, so a chain guessed here would be stages nobody agreed to."""
    for route_name in ("shape", "roadmap", "goal-setting", "pm-decision"):
        chain = sa.stages(route_name, "XL")
        assert chain["terminal"] is None, route_name
        assert len(chain["rows"]) == 1, route_name
        assert chain["owned_by"] == route_name


def test_route_returns_the_chain_for_the_RESIZED_tshirt():
    """A probe that moved the notch moves the terminal with it — reading the
    chain off the caller's original t-shirt is the bug this pins."""
    collapsed = sa.route(estimate={"tshirt": "M"}, probe_signal="collapse")
    assert collapsed["resolved_estimate"]["tshirt"] == "S"
    assert collapsed["route"] == "spec-dispatch"
    assert collapsed["stages"]["terminal"] == "quick-wrap"


def test_express_lane_still_carries_a_chain():
    """D3 persists no sizing object on this arm, but the ask still runs work and
    still closes. An absent field here would make absence mean two things."""
    result = sa.route(estimate={"tshirt": "XS"}, express_lane=True)
    assert result["stages"]["rows"] == ["the work", "quick-wrap"]


def test_stages_mutates_nothing_and_is_stable_across_calls():
    first = sa.stages("plan", "M")
    first["rows"].append("tampered")
    second = sa.stages("plan", "M")
    assert "tampered" not in second["rows"]
