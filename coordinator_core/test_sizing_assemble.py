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


def test_unknown_appetite_raises_usage_error():
    with pytest.raises(sa.SizingAssembleError):
        sa.route(appetite="huge", estimate={"tshirt": "M"})


def test_unknown_tshirt_raises_usage_error():
    with pytest.raises(sa.SizingAssembleError):
        sa.route(appetite="medium", estimate={"tshirt": "XXL"})


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


@pytest.mark.parametrize("tshirt,expected_route", [("L", "plan"), ("XL", "pm-decision")])
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
