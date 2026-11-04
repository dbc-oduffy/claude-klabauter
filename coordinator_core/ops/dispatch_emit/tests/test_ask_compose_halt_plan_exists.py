"""A halt names a plan only when it is on disk: an author who moves the plan after its path is minted."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import ask_contract as c

_MINTED = "docs/plans/2026-11/2026-11-04-hnd-carry-eb2760.md"
_WRITTEN = "docs/plans/2026-11-04-hnd-carry-eb2760.md"


def _halted(plan: str) -> dict:
    return {
        "halted": "commit",
        c.HALT_PLAN_FIELD: plan,
        "next_action": "Perform C3: coordinator-invoke plan.tasks.mutate "
        f'{{"verb":"em-discharge","plan_path":{json.dumps(plan)},"id":"C3"}}',
    }


def test_moved_plan_halt_names_no_missing_path(tmp_path):
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / _WRITTEN).write_text("plan\n", encoding="utf-8")

    got = c.halted_naming_existing_plan(tmp_path, _halted(_MINTED))

    assert got[c.HALT_PLAN_FIELD] is None
    assert _MINTED not in got["next_action"]
    assert c.HALT_MISSING_PLAN_PLACEHOLDER in got["next_action"]
    assert c.HALT_PLAN_TRAILER not in c.halted_record_message("r", got)


def test_existing_plan_halt_is_unchanged_and_trailer_names_it(tmp_path):
    (tmp_path / _WRITTEN).parent.mkdir(parents=True)
    (tmp_path / _WRITTEN).write_text("plan\n", encoding="utf-8")
    halted = _halted(_WRITTEN)

    got = c.halted_naming_existing_plan(tmp_path, halted)

    assert got is halted
    assert f"{c.HALT_PLAN_TRAILER}: {_WRITTEN}" in c.halted_record_message("r", got)


def test_halt_without_a_plan_or_with_structured_next_action_is_handled(tmp_path):
    bare = {"halted": "commit"}
    assert c.halted_naming_existing_plan(tmp_path, bare) is bare
    structured = {"halted": "commit", c.HALT_PLAN_FIELD: _MINTED, "next_action": {"kind": "refire", "call": _MINTED}}
    got = c.halted_naming_existing_plan(tmp_path, structured)
    assert got["next_action"] == {"kind": "refire", "call": c.HALT_MISSING_PLAN_PLACEHOLDER}
    assert got[c.HALT_PLAN_FIELD] is None


def test_plan_path_escaping_the_repo_counts_as_missing(tmp_path):
    got = c.halted_naming_existing_plan(tmp_path, _halted("../outside.md"))
    assert got[c.HALT_PLAN_FIELD] is None
