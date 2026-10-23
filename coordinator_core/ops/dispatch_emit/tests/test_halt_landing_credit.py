"""halt_landing.landing_credited and since_halt_close: the shared since-halt landing credit."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
from coordinator_core.ops.dispatch_emit.halt_landing import landing_credited, since_halt_close

LONG = "f665c40f92" + "a" * 30


def _v(met_by):
    return {"items": [{"id": "R1", "status": "met", "met_by": met_by}]}


def _fail(reason=UNCHANGED_SINCE_HALT_REASON):
    return [{"id": "R1", "reason": reason}]


def test_prose_met_by_credits():
    assert landing_credited(_v("Incident test passes"), ["R1"], LONG)


def test_short_sha_credits():
    assert landing_credited(_v("landed in f665c40"), ["R1"], LONG)


def test_long_sha_prefix_of_short_credits():
    assert landing_credited(_v(LONG), ["R1"], LONG[:12])


def test_unrelated_sha_refuses():
    assert not landing_credited(_v("landed in deadbeef12"), ["R1"], LONG)


def test_list_mixing_prose_and_matching_sha_credits():
    assert landing_credited(_v(["tests pass", "f665c40"]), ["R1"], LONG)


def test_other_rows_ignored():
    assert landing_credited(_v("deadbeef12"), ["R2"], LONG)


def test_since_halt_close_returns_sha():
    assert since_halt_close({"landed_sha": LONG}, _fail(), _v("tests pass")) == LONG


def test_since_halt_close_none_for_other_reason():
    assert since_halt_close({"landed_sha": LONG}, _fail("else"), _v("tests pass")) is None


def test_since_halt_close_none_without_landed_sha():
    assert since_halt_close({}, _fail(), _v("tests pass")) is None


def test_since_halt_close_none_on_contrary_citation():
    assert since_halt_close({"landed_sha": LONG}, _fail(), _v("deadbeef12")) is None


def test_since_halt_close_cited_detail_credits():
    assert since_halt_close({"landed_sha": LONG, "cited": {"R1": LONG}}, _fail(), _v("deadbeef12")) == LONG
