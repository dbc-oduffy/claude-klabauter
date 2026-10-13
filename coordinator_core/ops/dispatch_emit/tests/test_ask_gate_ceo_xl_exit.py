"""ask_gate in ceo mode: an open XL exit halts as a room the run may discharge in-run; pm does not."""

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
def test_other_modes_keep_the_plain_room_halt(repo, mode):  # noqa: F811
    _put(repo, tshirt="XL", route="pm-decision", mode=mode)
    assert "apm_xl_exit" not in gate(repo, REL).halt


def test_ceo_shape_and_goal_setting_stay_rooms_without_the_flag(repo):  # noqa: F811
    for route in ("shape", "goal-setting"):
        _put(repo, tshirt="XXL", route=route, mode="ceo")
        assert "apm_xl_exit" not in gate(repo, REL).halt


def test_ceo_recorded_multi_session_exit_proceeds_past_the_room(repo):  # noqa: F811
    _put(repo, tshirt="XL", route="pm-decision", mode="ceo", xl_exit="accept_multi_session")
    halt = gate(repo, REL).halt
    assert halt is None or halt["kind"] != "room"


def test_only_ceo_delegates_the_exit():
    assert apm_picks_xl_exit("ceo") and not apm_picks_xl_exit("pm") and not apm_picks_xl_exit("hands-on")
