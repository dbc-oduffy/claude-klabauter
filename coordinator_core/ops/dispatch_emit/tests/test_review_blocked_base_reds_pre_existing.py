"""A red measured at the run base stays pre_existing through the halt self-correction re-verify and never blocks."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.self_correction import (
    HALT_FIX_LABEL,
    HALT_REVERIFY_LABEL,
    classify_reds,
)

from .test_ask_compose_halt_self_correction import _OWN, _OWN_TEST, _OWNED, _done, _drive, _labels, _red

_BASE_RED = f"{_OWN_TEST}::test_red_at_base"


def test_classify_reds_a_base_red_in_the_write_set_is_foreign_and_a_run_only_red_is_owned():
    split = classify_reds([_BASE_RED, _OWNED], [_OWN_TEST], pre_existing=[_BASE_RED])

    assert split.foreign == (_BASE_RED,) and split.owned == (_OWNED,)
    assert split.unverified == ()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_base_red_survives_the_halt_reverify_as_pre_existing_and_the_run_lands_without_it():
    out = _drive(
        reverify=_red(_OWNED, _BASE_RED, pre_existing=[_BASE_RED]),
        replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [{"test": _red(_BASE_RED), "slices": []}]},
    )

    assert _labels(out) == [HALT_FIX_LABEL, HALT_REVERIFY_LABEL]
    assert out["halted"] is None
    assert _BASE_RED in out["reverify"]["pre_existing"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_run_only_red_still_blocks_while_the_base_red_beside_it_does_not():
    out = _drive(
        reverify=_red(_OWNED, _BASE_RED, pre_existing=[_BASE_RED]),
        replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [{"test": _red(_OWNED, _BASE_RED), "slices": []}]},
    )

    assert out["halted"]["halted"] == "review_blocked"
    assert [t["node_id"] for t in out["halted"]["failing"]] == [_OWNED]
    assert _BASE_RED not in str(out["halted"]["gaps"])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_red_never_measured_at_base_is_not_carried_as_pre_existing():
    out = _drive(
        reverify=_red(_OWNED),
        replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [{"test": _red(_BASE_RED), "slices": []}]},
    )

    assert _BASE_RED not in out["reverify"]["pre_existing"]
