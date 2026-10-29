"""ask_gate: a goal-setting sizing carrying xl_exit roadmap proceeds to the roadmap arm; without it, it stays a room."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_ROADMAP, effective_route, resolve_arm
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401


def test_goal_setting_with_roadmap_exit_is_not_a_room(repo):  # noqa: F811
    _put(repo, tshirt="XXL", route="goal-setting", mode="ceo", xl_exit="roadmap")
    v = gate(repo, REL)
    assert not (v.halt and v.halt["kind"] == "room")


def test_goal_setting_with_roadmap_exit_resolves_to_the_roadmap_arm():
    sizing = {"route": "goal-setting", "xl_exit": "roadmap", "estimate": {"tshirt": "XXL"}}
    assert effective_route(sizing) == "roadmap"
    assert resolve_arm(sizing) == ARM_ROADMAP


def test_goal_setting_with_a_landed_goal_is_not_a_room(repo):  # noqa: F811
    _put(repo, tshirt="XXL", route="goal-setting", mode="ceo", xl_exit=None, goal_id="goal-dense-head")
    v = gate(repo, REL)
    assert not (v.halt and v.halt["kind"] == "room")
    assert effective_route({"route": "goal-setting", "goal_id": "goal-dense-head"}) == "roadmap"


def test_goal_setting_with_a_blank_goal_id_still_halts_as_room(repo):  # noqa: F811
    _put(repo, tshirt="XXL", route="goal-setting", mode="ceo", xl_exit=None, goal_id="  ")
    v = gate(repo, REL)
    assert v.halt["kind"] == "room" and v.halt["route"] == "goal-setting"


def test_goal_setting_without_an_exit_still_halts_as_room(repo):  # noqa: F811
    _put(repo, tshirt="XXL", route="goal-setting", mode="ceo", xl_exit=None)
    v = gate(repo, REL)
    assert v.arm is None and v.halt["kind"] == "room" and v.halt["route"] == "goal-setting"


def test_goal_setting_with_a_non_roadmap_exit_still_halts_as_room(repo):  # noqa: F811
    _put(repo, tshirt="XXL", route="goal-setting", mode="ceo", xl_exit="accept_multi_session")
    v = gate(repo, REL)
    assert v.halt["kind"] == "room" and v.halt["route"] == "goal-setting"
