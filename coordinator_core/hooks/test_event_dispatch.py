"""Pins the hooks.dispatch_event contract: table shape, matcher resolution, merge rule."""

from __future__ import annotations

import re

from coordinator_core import _hook_envelope as env
from coordinator_core.hooks import event_dispatch as ed
from coordinator_core.ops._registry_map import OP_MODULE_MAP


def test_dispatch_event_stub_answers_no_advisory():
    assert ed.dispatch_event({"hook_event_name": "PreToolUse", "tool_name": "Bash"}) == {}


def test_table_shape_and_ops_are_registered():
    assert ed.EVENT_TABLE
    for row in ed.EVENT_TABLE:
        assert row.event in {"PreToolUse", "PostToolUse", "Stop"}
        re.compile(row.matcher)
        assert row.ops and all(op in OP_MODULE_MAP for op in row.ops)
        assert row.leg_class in {ed.BLOCKING, ed.RECONSTRUCTABLE}
        # An advisory row can never deny, so it cannot carry a blocking leg.
        assert not (row.advisory and row.leg_class == ed.BLOCKING)


def test_every_per_family_aggregator_is_seeded():
    seeded = {op for row in ed.EVENT_TABLE for op in row.ops}
    assert {
        "hooks.preuse_skill_dispatch",
        "hooks.agent_postuse_dispatch",
        "hooks.postuse_advisory_dispatch",
    } <= seeded


def test_matcher_resolution_is_a_full_match_and_empty_matches_all():
    assert ed.ops_for("PreToolUse", "Bash") == ("hooks.preuse_bash_dispatch",)
    assert ed.ops_for("PreToolUse", "PowerShell") == ("hooks.preuse_bash_dispatch",)
    assert ed.ops_for("PreToolUse", "BashX") == ()
    assert ed.ops_for("PreToolUse", "Skill") == ("hooks.preuse_skill_dispatch",)
    assert ed.ops_for("Stop", None) == ("hooks.stop_dispatch",)
    assert ed.ops_for("Stop", "anything") == ("hooks.stop_dispatch",)
    assert ed.ops_for("SessionStart", "Bash") == ()


def test_post_agent_runs_ordered_legs_once():
    ops = ed.ops_for("PostToolUse", "Agent")
    assert ops == ("hooks.postuse_advisory_dispatch", "hooks.agent_postuse_dispatch")
    assert len(ops) == len(set(ops))


def test_merge_first_deny_wins_and_short_circuits():
    first = env.deny("PreToolUse", "first")
    second = env.deny("PreToolUse", "second")
    advisory = env.context_only("PreToolUse", "note")
    assert ed.merge_envelopes("PreToolUse", [advisory, first, second]) is first


def test_merge_stop_block_is_a_deny():
    block = env.deny("Stop", "held")
    assert ed.merge_envelopes("Stop", [env.event_advisory("Stop", "fyi"), block]) is block


def test_merge_concatenates_advisories_in_order_with_one_stamp():
    merged = ed.merge_envelopes(
        "PostToolUse",
        [env.post_advisory("alpha"), {}, env.post_advisory("beta"), None],
    )
    text = merged["hookSpecificOutput"]["additionalContext"]
    assert text == "[coordinator] alpha\n\nbeta"
    assert merged["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_merge_stop_advisory_is_system_message_only():
    merged = ed.merge_envelopes("Stop", [env.event_advisory("Stop", "a"), env.event_advisory("Stop", "b")])
    assert merged == {"systemMessage": "[coordinator] a\n\nb"}


def test_merge_keeps_first_rewrite_and_folds_advisory_text():
    merged = ed.merge_envelopes(
        "PreToolUse",
        [
            env.context_only("PreToolUse", "note"),
            env.rewrite_input("PreToolUse", {"command": "one"}, "why one"),
            env.rewrite_input("PreToolUse", {"command": "two"}),
        ],
    )
    hso = merged["hookSpecificOutput"]
    assert hso["updatedInput"] == {"command": "one"}
    assert hso["additionalContext"] == "[coordinator] note\n\nwhy one"


def test_merge_nothing_is_no_advisory():
    assert ed.merge_envelopes("PreToolUse", []) == {}
    assert ed.merge_envelopes("PreToolUse", [{}, None, "x"]) == {}
