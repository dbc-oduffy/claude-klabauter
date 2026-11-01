"""ask_gate in ceo mode: an open XL exit halts as a room the run discharges in-run, in every mode."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.sizing_acceptance import apm_picks_xl_exit


@pytest.mark.parametrize("tshirt", ["XL", "XXL"])
def test_ceo_open_xl_exit_flags_the_in_run_pick(repo, tshirt):  # noqa: F811
    _put(repo, tshirt=tshirt, route="pm-decision", mode="ceo")
    v = gate(repo, REL)
    assert v.arm is None and v.halt["kind"] == "room" and v.halt["apm_xl_exit"] is True


@pytest.mark.parametrize("mode", ["pm", "hands-on"])
def test_other_modes_flag_the_in_run_pick(repo, mode):  # noqa: F811
    _put(repo, tshirt="XL", route="pm-decision", mode=mode)
    assert gate(repo, REL).halt["apm_xl_exit"] is True


def test_ceo_shape_and_goal_setting_stay_rooms_without_the_flag(repo):  # noqa: F811
    for route in ("shape", "goal-setting"):
        _put(repo, tshirt="XXL", route=route, mode="ceo")
        assert "apm_xl_exit" not in gate(repo, REL).halt


def test_ceo_recorded_multi_session_exit_proceeds_past_the_room(repo):  # noqa: F811
    (repo / ".git").mkdir()
    _put(repo, tshirt="XL", route="pm-decision", mode="ceo", xl_exit="accept_multi_session")
    halt = gate(repo, REL).halt
    assert halt is None or halt["kind"] != "room"


def test_recorded_split_halts_with_kind_split_never_an_arm(repo):  # noqa: F811
    _put(repo, tshirt="XL", route="pm-decision", mode="ceo", xl_exit="split")
    v = gate(repo, REL)
    assert v.arm is None and v.halt["kind"] == "split"


def test_every_mode_delegates_the_exit():
    assert all(apm_picks_xl_exit(m) for m in ("ceo", "pm", "hands-on"))
    assert not apm_picks_xl_exit(None) and not apm_picks_xl_exit("bogus")


@pytest.mark.parametrize("mode", ["ceo", "pm", "hands-on"])
def test_xl_exit_stage_opens_for_pm_decision_in_every_mode(repo, mode):  # noqa: F811
    from coordinator_core.ops.dispatch_emit.ask_compose import _xl_exit_open

    _put(repo, tshirt="XL", route="pm-decision", mode=mode)
    assert _xl_exit_open(str(repo), REL) is True
