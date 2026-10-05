"""plan-tasks 3.7.0 `deferred_until` temporary hold: schema shape and governed lint."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.frontmatter import schema_validate as sv

_SCHEMA = Path(sv.__file__).parent / "schemas" / "plan-tasks.schema.json"


def _row(**extra) -> dict:
    row = {"id": "C1", "title": "t", "surface": "s", "change_kind": "doctrine-edit", "writes": ["a.py"]}
    row.update(extra)
    return row


def _errors(row: dict) -> list:
    return sv.validate_frontmatter(row, _SCHEMA)


_HOLD = {"reason": "waits on X", "revisit_trigger": "X lands"}


def test_valid_hold_on_open_row_passes():
    assert _errors(_row(deferred_until=_HOLD)) == []


def test_extra_key_in_hold_is_rejected():
    assert _errors(_row(deferred_until={**_HOLD, "extra": "x"}))


def test_missing_trigger_is_rejected():
    assert _errors(_row(deferred_until={"reason": "r"}))


def test_hold_on_wont_do_row_is_rejected():
    assert _errors(_row(deferred_until=_HOLD, disposition="wont_do"))


def test_governed_deferral_rule_ignores_hold_and_names_it_in_hint():
    assert sv._cf_plan_tasks_unratified_deferral_governed(
        _row(deferred_until=_HOLD), governed=True
    ) is None
    err = sv._cf_plan_tasks_unratified_deferral_governed(_row(deferred=True), governed=True)
    assert err and "deferred_until" in err["hint"]
    assert sv.is_unratified_deferral(_row(deferred_until=_HOLD), governed=True) is False
