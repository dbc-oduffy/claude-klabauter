"""identical_rehalt_reason: the dd822d second-refire shape is true; each release condition is false."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_DISPATCH,
    LEG_SKIP,
    RESUME_LEDGER_CREDITED_FIELD,
    RESUME_LEDGER_HALT_SHA_FIELD,
    RESUME_LEDGER_KIND_FIELD,
    RESUME_LEDGER_NOTES_FIELD,
    RESUME_LEDGER_SINCE_HALT_SHAS_FIELD,
)
from coordinator_core.ops.dispatch_emit.refire_noop import identical_rehalt_reason

SHA = "a" * 40


def _ledger(**over):
    base = {
        "blocking": ["R1"],
        RESUME_LEDGER_HALT_SHA_FIELD: SHA,
        RESUME_LEDGER_NOTES_FIELD: [],
        RESUME_LEDGER_SINCE_HALT_SHAS_FIELD: [],
        RESUME_LEDGER_CREDITED_FIELD: {},
    }
    base.update(over)
    return base


def _call(ledger=None, rows=(), owed=(), gated=(), derived=(), leg=None):
    return identical_rehalt_reason(_ledger() if ledger is None else ledger, rows, owed, gated, derived, leg)


def test_dd822d_second_refire_shape_is_identical_rehalt():
    reason = _call(leg={"action": LEG_SKIP})
    assert reason is not None
    assert "R1" in reason and SHA[:10] in reason and "workflow.revise" in reason


def test_all_landed_kind_also_true():
    assert _call(_ledger(**{RESUME_LEDGER_KIND_FIELD: "all-landed"})) is not None


def test_no_leg_is_true():
    assert _call(leg=None) is not None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rows": [{"id": "C5"}]},
        {"owed": ["C5"]},
        {"gated": ["C6"]},
        {"derived": ["LR1"]},
        {"leg": {"action": LEG_DISPATCH}},
        {"ledger": _ledger(**{RESUME_LEDGER_NOTES_FIELD: ["rule: add gem_holder"]})},
        {"ledger": _ledger(**{RESUME_LEDGER_SINCE_HALT_SHAS_FIELD: [SHA]})},
        {"ledger": _ledger(**{RESUME_LEDGER_CREDITED_FIELD: {"R1": SHA}})},
        {"ledger": _ledger(blocking=[])},
        {"ledger": _ledger(**{RESUME_LEDGER_KIND_FIELD: "commit"})},
    ],
)
def test_each_release_condition_is_none(kwargs):
    assert _call(**kwargs) is None


def test_no_resume_ledger_is_none():
    assert identical_rehalt_reason(None, (), (), (), (), None) is None
