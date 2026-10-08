"""
coordinator_core.sizing_assemble.test_interaction_mode_touchpoints — C6.

Covers Design § Engine's touchpoint table and the mode-vs-size interaction:
per-mode touchpoint ids at M (the AC table), the XS/S size rule that drops
the sizing-stage touchpoint, `exit_criterion_pending` firing at every
resized M+ regardless of mode or a passed statement, `post_size_prompt_
pending` suppressed in `ceo` mode and, outside hands-on, wherever the engine
skips sizing acceptance (plan/dispatch XS-L), and route/stages equality across
all three modes against a recorded table (never a re-derivation of C0's
route logic).

Spec: docs/plans/2026-09-27-sizing-carries-exit-criterion-and-interaction-mode.md § C6.
"""
from __future__ import annotations

import itertools

import pytest

import coordinator_core.sizing_assemble as sa
from coordinator_core.session.mode_resolution import INTERACTION_MODES


def _touchpoint_ids(mode: str, tshirt: str, route: str | None = None) -> list[str]:
    route = route or sa._BASE_ROUTE_BY_TSHIRT[tshirt]
    return [t["id"] for t in sa.touchpoints(mode, tshirt, route)]


class TestTouchpointTableIsTotal:
    def test_local_mirror_matches_mode_resolution_canonical(self):
        # C6's local `_INTERACTION_MODES_EXPECTED` mirror must never drift
        # from C2's canonical registry order.
        assert sa._INTERACTION_MODES_EXPECTED == INTERACTION_MODES

    def test_touchpoints_by_mode_total_over_every_mode(self):
        assert set(sa.TOUCHPOINTS_BY_MODE) == set(INTERACTION_MODES)


class TestTouchpointsAtM:
    # `plan` is M's resolved route; a keeping route is exercised in the last test.
    def test_pm_at_m(self):
        assert _touchpoint_ids("pm", "M", "plan") == ["accept_result"]

    def test_hands_on_at_m(self):
        assert _touchpoint_ids("hands-on", "M", "plan") == ["execute_go", "wrap_up"]

    def test_ceo_at_m(self):
        assert _touchpoint_ids("ceo", "M", "plan") == []

    def test_modes_at_m_on_a_keeping_route(self):
        assert _touchpoint_ids("pm", "M", "shape") == ["accept_sizing", "accept_result"]
        assert _touchpoint_ids("ceo", "M", "pm-decision") == ["accept_exit_criterion"]


class TestTouchpointsAtS:
    def test_pm_at_s_drops_sizing_stage(self):
        assert _touchpoint_ids("pm", "S") == ["accept_result"]

    def test_hands_on_at_s_drops_sizing_stage(self):
        assert _touchpoint_ids("hands-on", "S") == ["execute_go", "wrap_up"]

    def test_ceo_at_s_is_empty(self):
        assert _touchpoint_ids("ceo", "S") == []

    def test_xs_matches_s(self):
        for mode in INTERACTION_MODES:
            assert _touchpoint_ids(mode, "XS") == _touchpoint_ids(mode, "S")


class TestAgentRunSizingRule:
    """PM ruling 2026-10-08: plan/dispatch at XS-L skip the sizing-stage touchpoints in every mode."""

    @pytest.mark.parametrize("mode", list(INTERACTION_MODES))
    @pytest.mark.parametrize(
        "route,tshirt", [("plan", "L"), ("plan", "M"), ("dispatch", "M"), ("dispatch", "XS")]
    )
    def test_plan_and_dispatch_to_l_drop_sizing_stage(self, mode, route, tshirt):
        ids = _touchpoint_ids(mode, tshirt, route)
        assert not {"accept_sizing", "accept_exit_criterion"} & set(ids)

    @pytest.mark.parametrize("mode", list(INTERACTION_MODES))
    @pytest.mark.parametrize(
        "route,tshirt",
        [("plan", "XL"), ("plan", "XXL"), ("shape", "S"), ("shape", "L"), ("pm-decision", "L"),
         ("pm-decision", "XL"), ("roadmap", "M"), ("goal-setting", "XXL")],
    )
    def test_everything_else_keeps_its_touchpoints(self, mode, route, tshirt):
        assert _touchpoint_ids(mode, tshirt, route) == [
            t["id"] for t in sa.TOUCHPOINTS_BY_MODE[mode]
        ]

    @pytest.mark.parametrize("mode", list(INTERACTION_MODES))
    def test_route_threads_the_resolved_route_into_the_decision(self, mode):
        l_plan = sa.route(estimate={"tshirt": "L"}, interaction_mode=mode)
        assert not {"accept_sizing", "accept_exit_criterion"} & {t["id"] for t in l_plan["touchpoints"]}
        assert "exit_criterion_pending" not in l_plan["detents"]
        xl = sa.route(estimate={"tshirt": "XL"}, interaction_mode=mode)
        assert "exit_criterion_pending" in xl["detents"]

    def test_shape_resolved_from_unclear_jtbd_at_l_keeps_the_ask(self):
        d = sa.route(estimate={"tshirt": "L"}, jtbd_unclear=True, interaction_mode="pm")
        assert d["route"] == "shape" and "exit_criterion_pending" in d["detents"]
        assert [t["id"] for t in d["touchpoints"]][0] == "accept_sizing"


class TestExitCriterionPendingDetent:
    @pytest.mark.parametrize("tshirt", ["XL", "XXL"])
    @pytest.mark.parametrize("mode", list(INTERACTION_MODES))
    def test_fires_at_every_resized_m_plus_in_every_mode(self, tshirt, mode):
        decision = sa.route(estimate={"tshirt": tshirt}, interaction_mode=mode)
        assert "exit_criterion_pending" in decision["detents"]

    @pytest.mark.parametrize("tshirt", ["XS", "S", "M", "L"])
    @pytest.mark.parametrize("mode", list(INTERACTION_MODES))
    def test_absent_where_the_engine_skips_acceptance(self, tshirt, mode):
        decision = sa.route(estimate={"tshirt": tshirt}, interaction_mode=mode)
        assert "exit_criterion_pending" not in decision["detents"]

    def test_absent_on_express_lane(self):
        decision = sa.route(
            estimate={"tshirt": "XL"}, express_lane=True, interaction_mode="pm"
        )
        assert "exit_criterion_pending" not in decision["detents"]

    def test_fires_even_when_a_statement_was_passed(self):
        decision = sa.route(
            estimate={"tshirt": "XL"}, exit_criterion="Ship the thing", interaction_mode="pm"
        )
        assert "exit_criterion_pending" in decision["detents"]
        assert decision["exit_criterion"] == {"statement": "Ship the thing", "accepted": None}

    def test_absent_exit_criterion_is_none(self):
        decision = sa.route(estimate={"tshirt": "M"})
        assert decision["exit_criterion"] is None


class TestPostSizePromptSuppressedInCeoOnly:
    @pytest.mark.parametrize("tshirt", ["M", "L", "XL", "XXL"])
    def test_ceo_never_sets_post_size_prompt_pending(self, tshirt):
        decision = sa.route(estimate={"tshirt": tshirt}, interaction_mode="ceo")
        assert "post_size_prompt_pending" not in decision["detents"]

    @pytest.mark.parametrize("tshirt", ["M", "L", "XL", "XXL"])
    def test_hands_on_keeps_post_size_prompt_pending_at_every_size(self, tshirt):
        decision = sa.route(estimate={"tshirt": tshirt}, interaction_mode="hands-on")
        assert "post_size_prompt_pending" in decision["detents"]

    @pytest.mark.parametrize("tshirt", ["XL", "XXL"])
    def test_pm_keeps_post_size_prompt_pending_at_xl_plus(self, tshirt):
        decision = sa.route(estimate={"tshirt": tshirt}, interaction_mode="pm")
        assert "post_size_prompt_pending" in decision["detents"]

    @pytest.mark.parametrize("tshirt", ["M", "L"])
    def test_pm_drops_post_size_prompt_where_acceptance_skipped(self, tshirt):
        decision = sa.route(estimate={"tshirt": tshirt}, interaction_mode="pm")
        assert decision["route"] == "plan"
        assert "post_size_prompt_pending" not in decision["detents"]
        assert "shall we go with that" not in decision["next_move"]

    @pytest.mark.parametrize("mode", ["hands-on", "pm", "ceo"])
    def test_shape_route_keeps_prompt_except_ceo(self, mode):
        decision = sa.route(
            estimate={"tshirt": "L"}, interaction_mode=mode, jtbd_unclear=True
        )
        assert decision["route"] == "shape"
        assert ("post_size_prompt_pending" in decision["detents"]) == (mode != "ceo")

    def test_pm_decision_at_xl_keeps_prompt_in_pm_mode(self):
        decision = sa.route(estimate={"tshirt": "XL"}, interaction_mode="pm")
        assert decision["route"] == "pm-decision"
        assert "post_size_prompt_pending" in decision["detents"]

    def test_pm_decision_pending_unchanged_across_modes(self):
        for mode in INTERACTION_MODES:
            decision = sa.route(estimate={"tshirt": "XL"}, interaction_mode=mode)
            assert "pm_decision_pending" in decision["detents"]

    def test_goal_setting_pm_gated_unchanged_across_modes(self):
        for mode in INTERACTION_MODES:
            decision = sa.route(estimate={"tshirt": "XXL"}, interaction_mode=mode)
            assert "goal_setting_pm_gated" in decision["detents"]


# --- route/stages equality across modes, against a recorded table -----------

_TSHIRTS = ("XS", "S", "M", "L", "XL", "XXL")
_PROBE_SIGNALS = (None, "collapse", "raise")
_APPETITES = (None, "small", "medium", "large")
_JTBD_UNCLEAR = (False, True)
_WELL_TRODDEN = (False, True)


def _recorded_route_and_stages(tshirt, probe_signal, appetite, jtbd_unclear, well_trodden):
    """The mode-blind decision C0 already resolves -- recorded once here as
    the fixture table, never re-derived per mode."""
    kwargs = dict(
        estimate={"tshirt": tshirt},
        probe_signal=probe_signal,
        appetite=appetite,
        jtbd_unclear=jtbd_unclear,
        well_trodden_step_change=well_trodden,
    )
    decision = sa.route(**kwargs, interaction_mode="hands-on")
    return decision["route"], decision["stages"]


class TestRouteAndStagesEqualAcrossModes:
    @pytest.mark.parametrize(
        "tshirt,probe_signal,appetite,jtbd_unclear,well_trodden",
        list(
            itertools.product(
                _TSHIRTS, _PROBE_SIGNALS, _APPETITES, _JTBD_UNCLEAR, _WELL_TRODDEN
            )
        ),
    )
    def test_route_and_stages_unaffected_by_mode(
        self, tshirt, probe_signal, appetite, jtbd_unclear, well_trodden
    ):
        expected_route, expected_stages = _recorded_route_and_stages(
            tshirt, probe_signal, appetite, jtbd_unclear, well_trodden
        )
        for mode in INTERACTION_MODES:
            decision = sa.route(
                estimate={"tshirt": tshirt},
                probe_signal=probe_signal,
                appetite=appetite,
                jtbd_unclear=jtbd_unclear,
                well_trodden_step_change=well_trodden,
                interaction_mode=mode,
            )
            assert decision["route"] == expected_route
            assert decision["stages"] == expected_stages


class TestUnknownInteractionModeRaises:
    def test_unknown_mode_raises(self):
        with pytest.raises(sa.SizingAssembleError):
            sa.route(estimate={"tshirt": "M"}, interaction_mode="boss")

    def test_default_mode_is_hands_on(self):
        decision = sa.route(estimate={"tshirt": "M"})
        assert decision["interaction_mode"] == "hands-on"
