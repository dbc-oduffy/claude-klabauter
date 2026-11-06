"""The review wave binds a grouped reply's slice ids itself; no prompt asks an agent to join them."""

from coordinator_core.ops.review_mint import execute_review as er
from coordinator_core.ops.review_mint.tests.test_execute_review import (
    _FULL,
    _ratio_review,
    _run_in_wave,
    _wave_block,
)

_BIND_SEPARATE = "r.slice_id = g.map(s => s.id).join('+')"
_BIND_IN_WAVE = "r.review.slice_id = _own.map(_sid).join('+')"


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


def _prep_rewrite(body):
    return f"(r, n) => n === 1 ? {{ ...r, prep: {{ ...r.prep, slices: {body} }} }} : r"


def test_a_prep_naming_the_whole_diffs_slices_beyond_its_dispatched_group_is_dropped_with_an_advisory():
    result = _run_in_wave(_FULL, 9, patch=_prep_rewrite("_full.slices"))
    assert "halted" not in result and "error" not in result
    assert result["wave"][1]["slice_id"] == _bound_group_one("r.prep.slices")
    [advisory] = result["prep"]["advisories"]
    assert "wave agent 1" in advisory and "outside its group 1 of" in advisory


def test_a_prep_naming_a_slice_id_no_run_slice_carries_halts():
    patch = _prep_rewrite("r.prep.slices.map(s => ({ ...s, id: s.id + 'x' }))")
    result = _run_in_wave(_FULL, 9, patch=patch)
    assert result["halted"].startswith(er.SLICE_ID_UNKNOWN_HALT)


def test_every_prep_naming_exactly_its_dispatched_group_is_rebound_without_halt():
    result = _run_in_wave(_FULL, 9)
    assert "halted" not in result and "error" not in result
    assert result["wave"][0]["slice_id"] == "s0+s1"


def _bound_group_one(body):
    result = _run_in_wave(_FULL, 9, patch=_prep_rewrite(body))
    assert "halted" not in result and "error" not in result
    return result["wave"][1]["slice_id"]


def test_a_prep_naming_one_member_is_bound_to_the_whole_dispatched_group():
    expected = _bound_group_one("r.prep.slices")
    assert _bound_group_one("r.prep.slices.slice(0, 1)") == expected
    assert _bound_group_one("r.prep.slices.slice(1, 2)") == expected


def test_a_prep_naming_reordered_members_is_bound_in_group_order():
    expected = _bound_group_one("r.prep.slices")
    assert _bound_group_one("[...r.prep.slices].reverse()") == expected


def test_a_prep_listing_member_ids_as_strings_is_bound_to_the_group():
    expected = _bound_group_one("r.prep.slices")
    assert _bound_group_one("r.prep.slices.map(s => s.id)") == expected


def test_a_prep_naming_a_non_member_alongside_a_member_keeps_the_verdict_and_advises():
    result = _run_in_wave(_FULL, 9, patch=_prep_rewrite("[r.prep.slices[0], _full.slices[_full.slices.length - 1]]"))
    assert "halted" not in result and "error" not in result
    assert result["wave"][1] is not None
    assert result["wave"][1]["slice_id"] == _bound_group_one("r.prep.slices")
    [advisory] = result["prep"]["advisories"]
    assert _full_last_id() in advisory


def test_a_prep_naming_only_foreign_slices_is_still_bound_to_its_group():
    expected = _bound_group_one("r.prep.slices")
    result = _run_in_wave(_FULL, 9, patch=_prep_rewrite("[_full.slices[_full.slices.length - 1]]"))
    assert "halted" not in result and result["wave"][1]["slice_id"] == expected
    assert len(result["prep"]["advisories"]) == 1


def test_an_id_no_run_slice_carries_still_halts_even_beside_a_member():
    patch = _prep_rewrite("[r.prep.slices[0], { ...r.prep.slices[0], id: 'zz-nonexistent' }]")
    result = _run_in_wave(_FULL, 9, patch=patch)
    assert result["halted"].startswith(er.SLICE_ID_UNKNOWN_HALT)


def test_a_clean_wave_carries_no_advisories():
    assert "advisories" not in _run_in_wave(_FULL, 9)["prep"]


def _full_last_id():
    return _FULL["slices"][-1]["id"]


def test_unbound_reply_still_falls_back_to_resolution_and_halt():
    block = _wave_block(_ratio_review(), executor_rows=9, prep_in_wave=True)
    assert "const _resolveSliceId" in block
    assert er.SLICE_ID_UNKNOWN_HALT in block
