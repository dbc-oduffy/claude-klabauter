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
2026-08-05-example-doctrine-repo-em-premise-provenance-detent-sizing-assemble.md):
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
    # xxl_unprobed -- the missed cell example-doctrine-repo's reply named.
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


@pytest.mark.parametrize("tshirt", ["XS", "S", "M"])
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


def test_premise_unproven_keys_on_resized_size_not_pre_resize_size():
    # An S estimate raised to L by the probe must still fire the detent —
    # keyed on the RESIZED size, never the pre-resize tshirt (memo: "key it
    # on resized SIZE, not resolved ROUTE" — this asserts the size half of
    # that same requirement).
    decision = sa.route(
        appetite="large",
        estimate={"tshirt": "S"},
        probe_signal="raise",
        premise_provenance="read",
    )
    assert decision["resolved_estimate"]["tshirt"] == "M"
    assert "premise_unproven" not in decision["detents"]

    decision2 = sa.route(
        appetite="large",
        estimate={"tshirt": "M"},
        probe_signal="raise",
        premise_provenance="read",
    )
    assert decision2["resolved_estimate"]["tshirt"] == "L"
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


def test_absent_appetite_next_move_suppressed_at_pm_decision():
    # AC6a: at XL, post_size_prompt_pending still fires as a detent, but the
    # next_move open-question append is suppressed because pm-decision's own
    # branch already asks a PM question (precedence rule #1 — ONE ask).
    decision = sa.route(estimate={"tshirt": "XL"})
    assert decision["route"] == "pm-decision"
    assert "post_size_prompt_pending" in decision["detents"]
    assert "shall we go with that or want to split it" not in decision["next_move"]


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
    # AC2 — the invariance test example-doctrine-repo explicitly asked for: for every t-shirt,
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
        pytest.skip("sibling example-doctrine-repo checkout not found")
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
    assert schema["x-schema-version"] == "1.7.0"
    assert schema["x-bump-class"] == "enum-value-additive"


def test_completion_entry_schema_version_and_bump_class():
    # AC13.
    import json
    import pathlib

    schema = json.loads(
        pathlib.Path(
            "coordinator_core/frontmatter/schemas/completion-entry.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["x-schema-version"] == "1.3.0"
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
    ]

    completion_schema = json.loads(
        pathlib.Path(
            "coordinator_core/frontmatter/schemas/completion-entry.schema.json"
        ).read_text(encoding="utf-8")
    )
    tshirt_arm = completion_schema["properties"]["loe"]["properties"]["tshirt"]["anyOf"][0]
    assert tshirt_arm["enum"] == ["XS", "S", "M", "L", "XL", "XXL"]


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

    text = pathlib.Path("coordinator/bin/coordinator-doc-new").read_text(
        encoding="utf-8"
    )
    assert "### Weak OOS / hedges (appetite-based deferrals)" in text
