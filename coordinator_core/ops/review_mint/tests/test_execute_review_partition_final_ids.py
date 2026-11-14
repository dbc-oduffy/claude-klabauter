"""A wave reply naming a pre-partition slice id is reconciled to its group, never halting a met run."""

import pytest

from coordinator_core.ops.review_mint import execute_review as er
from coordinator_core.ops.review_mint.tests.test_execute_review import (
    _FULL,
    _ratio_review,
    _run_in_wave,
    _wave_block,
)

pytestmark = pytest.mark.complete

_FINAL_ID = "plan-000ee065029a-prep-3"
_STALE_IDS = ["plan-000ee065029a-prep-1", "plan-000ee065029a-prep-2"]
_ONE = {**_FULL, "slices": [{**_FULL["slices"][0], "id": _FINAL_ID}]}


def _stale_reply(retry_at):
    stale = ", ".join(f"{{ ...r.prep.slices[0], id: {sid!r} }}" for sid in _STALE_IDS)
    return (
        f"(r, n) => (n === 0 || n === {retry_at}) && r.prep.slices.length ? "
        f"{{ ...r, prep: {{ ...r.prep, slices: [{stale}] }}, review: {{ ...r.review, slice_id: {_STALE_IDS[0]!r} }} }} : r"
    )


def test_a_final_list_dropping_pre_partition_ids_with_a_stale_group_reply_does_not_halt():
    spawned = len(_run_in_wave(_ONE, 9)["spawned"])
    result = _run_in_wave(_ONE, 9, patch=_stale_reply(spawned))
    assert "halted" not in result and "error" not in result
    assert result["wave"][0]["slice_id"] == _FINAL_ID
    assert [s["id"] for s in result["prep"]["slices"]] == [_FINAL_ID]


def test_the_stray_pre_partition_ids_are_named_in_an_advisory_and_never_counted_as_coverage():
    spawned = len(_run_in_wave(_ONE, 9)["spawned"])
    result = _run_in_wave(_ONE, 9, patch=_stale_reply(spawned))
    [advisory] = result["prep"]["advisories"]
    assert all(sid in advisory for sid in _STALE_IDS)
    assert all(sid not in s["id"] for s in result["prep"]["slices"] for sid in _STALE_IDS)
    assert all(sid not in (w or {}).get("slice_id", "") for w in result["wave"] for sid in _STALE_IDS)


def test_a_clean_single_slice_run_binds_the_final_id_with_no_advisory():
    result = _run_in_wave(_ONE, 9)
    assert result["wave"][0]["slice_id"] == _FINAL_ID
    assert "advisories" not in result["prep"]


def test_a_range_reply_still_halts_naming_the_range():
    spawned = len(_run_in_wave(_ONE, 9)["spawned"])
    patch = (
        f"(r, n) => (n === 0 || n === {spawned}) ? {{ ...r, prep: {{ ...r.prep, slices: "
        "[{ id: 'plan-prep-1..plan-prep-3' }] } } : r"
    )
    result = _run_in_wave(_ONE, 9, patch=patch)
    assert result["halted"].startswith(er.SLICE_ID_UNKNOWN_HALT)
    assert "plan-prep-1..plan-prep-3" in result["halted"]


def test_group_briefs_carry_the_group_position_and_no_slice_id_literal():
    block = _wave_block(_ratio_review(), executor_rows=9, prep_in_wave=True)
    assert "group_index: " in block and "group_count: " in block
    assert _FINAL_ID not in block and "-prep-1" not in block
