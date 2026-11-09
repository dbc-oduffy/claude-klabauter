"""A two-slice prep dispatched as one merged group accepts a reply naming its members or their '+'-joined id."""

import pytest

from coordinator_core.ops.review_mint import execute_review as er
from coordinator_core.ops.review_mint.tests.test_execute_review import _FULL, _run_in_wave

pytestmark = pytest.mark.complete

_TWO = {**_FULL, "slices": _FULL["slices"][:2]}
_IDS = [s["id"] for s in _TWO["slices"]]
_JOINED = "+".join(_IDS)


def _reply_slices(body):
    return f"(r, n) => n === 0 ? {{ ...r, prep: {{ ...r.prep, slices: {body} }} }} : r"


def _accepted(body):
    result = _run_in_wave(_TWO, 1, patch=_reply_slices(body))
    assert "halted" not in result and "error" not in result
    assert result["wave"][0]["slice_id"] == _JOINED
    return result


def test_a_reply_naming_both_member_ids_is_accepted_and_bound_to_the_joined_id():
    _accepted("r.prep.slices.map(s => ({ id: s.id }))")


def test_a_reply_naming_the_members_as_strings_is_accepted():
    _accepted("r.prep.slices.map(s => s.id)")


def test_a_reply_naming_the_plus_joined_id_is_accepted_and_bound_to_the_members():
    result = _accepted(f"[{{ id: {_JOINED!r} }}]")
    assert [s["id"] for s in result["prep"]["slices"]] == _IDS


def test_a_reply_naming_the_plus_joined_id_as_a_string_is_accepted():
    _accepted(f"[{_JOINED!r}]")


def test_an_id_outside_the_merged_group_still_halts():
    patch = _reply_slices("[{ id: 'manifest-x-prep-1' }, { id: 'manifest-x-prep-2' }]")
    assert _run_in_wave(_TWO, 1, patch=patch)["halted"].startswith(er.SLICE_ID_UNKNOWN_HALT)


def test_a_joined_id_naming_a_non_member_still_halts():
    patch = _reply_slices(f"[{{ id: {_IDS[0] + '+zz-nonexistent'!r} }}]")
    assert _run_in_wave(_TWO, 1, patch=patch)["halted"].startswith(er.SLICE_ID_UNKNOWN_HALT)
