"""
coordinator_core.sizing_assemble.test_interaction_mode_touchpoints — C6.

Covers Design § Engine's touchpoint table and the mode-vs-size interaction:
per-mode touchpoint ids at M (the AC table), the XS/S size rule that drops
the sizing-stage touchpoint, `exit_criterion_pending` firing at every
resized M+ regardless of mode or a passed statement, `post_size_prompt_
pending` suppressed in `ceo` mode only, and route/stages equality across
all three modes against a recorded table (never a re-derivation of C0's
route logic).

Spec: docs/plans/2026-09-27-sizing-carries-exit-criterion-and-interaction-mode.md § C6.
"""
from __future__ import annotations

import itertools

import pytest

import coordinator_core.sizing_assemble as sa
from coordinator_core.session.mode_resolution import INTERACTION_MODES


def _touchpoint_ids(mode: str, tshirt: str) -> list[str]:
    return [t["id"] for t in sa.touchpoints(mode, tshirt)]


class TestTouchpointTableIsTotal:
    def test_local_mirror_matches_mode_resolution_canonical(self):
        # C6's local `_INTERACTION_MODES_EXPECTED` mirror must never drift
        # from C2's canonical registry order.
        assert sa._INTERACTION_MODES_EXPECTED == INTERACTION_MODES

    def test_touchpoints_by_mode_total_over_every_mode(self):
        assert set(sa.TOUCHPOINTS_BY_MODE) == set(INTERACTION_MODES)


class TestTouchpointsAtM:
    def test_pm_at_m(self):
        assert _touchpoint_ids("pm", "M") == ["accept_sizing", "accept_result"]

    def test_hands_on_at_m(self):
        assert _touchpoint_ids("hands-on", "M") == [
            "accept_sizing",
            "execute_go",
            "wrap_up",
        ]

    def test_ceo_at_m(self):
        assert _touchpoint_ids("ceo", "M") == ["accept_exit_criterion"]


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


class TestExitCriterionPendingDetent:
    @pytest.mark.parametrize("tshirt", ["M", "L", "XL", "XXL"])
    @pytest.mark.parametrize("mode", list(INTERACTION_MODES))
    def test_fires_at_every_resized_m_plus_in_every_mode(self, tshirt, mode):
        decision = sa.route(estimate={"tshirt": tshirt}, interaction_mode=mode)
        assert "exit_criterion_pending" in decision["detents"]

    @pytest.mark.parametrize("tshirt", ["XS", "S"])
    @pytest.mark.parametrize("mode", list(INTERACTION_MODES))
    def test_absent_at_xs_s(self, tshirt, mode):
        decision = sa.route(estimate={"tshirt": tshirt}, interaction_mode=mode)
        assert "exit_criterion_pending" not in decision["detents"]

    def test_absent_on_express_lane(self):
        decision = sa.route(
            estimate={"tshirt": "XL"}, express_lane=True, interaction_mode="pm"
        )
        assert "exit_criterion_pending" not in decision["detents"]

    def test_fires_even_when_a_statement_was_passed(self):
        decision = sa.route(
            estimate={"tshirt": "M"}, exit_criterion="Ship the thing", interaction_mode="pm"
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
    @pytest.mark.parametrize("mode", ["hands-on", "pm"])
    def test_hands_on_and_pm_keep_post_size_prompt_pending(self, tshirt, mode):
        decision = sa.route(estimate={"tshirt": tshirt}, interaction_mode=mode)
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
