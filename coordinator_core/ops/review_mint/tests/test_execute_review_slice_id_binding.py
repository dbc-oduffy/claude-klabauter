"""The review wave binds a grouped reply's slice ids itself; no prompt asks an agent to join them."""

from coordinator_core.ops.review_mint import execute_review as er
from coordinator_core.ops.review_mint.tests.test_execute_review import (
    _ratio_review,
    _wave_block,
)

_BIND_SEPARATE = "r.slice_id = g.map(s => s.id).join('+')"
_BIND_IN_WAVE = "r.review.slice_id = _own.map(s => s.id).join('+')"


def test_no_clause_asks_an_agent_to_join_slice_ids():
    assert "joined with" not in er._GROUPED_SLICES_CLAUSE
    assert "joined with" not in er._WAVE_GROUP_CLAUSE
    assert "first listed slice's id" in er._GROUPED_SLICES_CLAUSE
    assert "first slice the reply lists" in er._WAVE_GROUP_CLAUSE


def test_separate_prep_grouped_thunk_binds_its_own_group_ids_after_the_reply():
    block = _wave_block(_ratio_review(), executor_rows=9)
    assert ".map(g => () => (async () => { try { return await agent(" in block
    assert _BIND_SEPARATE in block
    assert block.index("_groupSlices(") < block.index(_BIND_SEPARATE)
    assert "joined with" not in block


def test_prep_in_wave_binds_slice_id_from_the_agents_own_prep_before_resolution():
    block = _wave_block(_ratio_review(), executor_rows=9, prep_in_wave=True)
    assert _BIND_IN_WAVE in block
    assert block.index(_BIND_IN_WAVE) < block.index("const _resolveSliceId")
    assert "!r.prep.error" in block.split(_BIND_IN_WAVE)[0].rsplit("for (const r of _waveRaw)", 1)[-1]
    assert "joined with" not in block


def test_unbound_reply_still_falls_back_to_resolution_and_halt():
    block = _wave_block(_ratio_review(), executor_rows=9, prep_in_wave=True)
    assert "const _resolveSliceId" in block
    assert er.SLICE_ID_UNKNOWN_HALT in block
