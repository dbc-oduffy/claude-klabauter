"""Successive halts of one sizing against a moved plan record one `plan` value."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as c

_MONTH = "docs/plans/2026-11/2026-11-04-hnd-carry-eb2760.md"
_FLAT = "docs/plans/2026-11-04-hnd-carry-eb2760.md"


def _halted(plan: str) -> dict:
    return {
        "halted": "commit",
        c.HALT_PLAN_FIELD: plan,
        "next_action": f'plan.tasks.mutate {{"plan_path":{json.dumps(plan)},"id":"C3"}}',
    }


def _put(tmp_path, rel: str) -> None:
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("plan\n", encoding="utf-8")


@pytest.mark.parametrize("on_disk,other", [(_FLAT, _MONTH), (_MONTH, _FLAT)])
def test_two_halts_name_one_plan_whichever_form_each_minted(tmp_path, on_disk, other):
    _put(tmp_path, on_disk)

    first = c.halted_naming_existing_plan(tmp_path, _halted(other))
    second = c.halted_naming_existing_plan(tmp_path, _halted(on_disk))

    assert first[c.HALT_PLAN_FIELD] == second[c.HALT_PLAN_FIELD] == on_disk
    assert other not in first["next_action"]
    assert on_disk in first["next_action"]
    assert f"{c.HALT_PLAN_TRAILER}: {on_disk}" in c.halted_record_message("r", first)


def test_plan_on_neither_form_is_still_nulled(tmp_path):
    got = c.halted_naming_existing_plan(tmp_path, _halted(_MONTH))

    assert got[c.HALT_PLAN_FIELD] is None
    assert c.HALT_MISSING_PLAN_PLACEHOLDER in got["next_action"]
