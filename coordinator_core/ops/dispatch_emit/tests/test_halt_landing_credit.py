"""halt_landing.landing_credited: the shared since-halt landing credit (``since_halt_close`` is pinned in test_since_halt_close_entrypoint)."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.halt_landing import landing_credited

LONG = "f665c40f92" + "a" * 30


def _v(met_by):
    return {"items": [{"id": "R1", "status": "met", "met_by": met_by}]}


def test_prose_met_by_credits():
    assert landing_credited(_v("Incident test passes"), ["R1"], LONG)


def test_short_sha_credits():
    assert landing_credited(_v("landed in f665c40"), ["R1"], LONG)


def test_long_sha_prefix_of_short_credits():
    assert landing_credited(_v(LONG), ["R1"], LONG[:12])


def test_unrelated_sha_refuses():
    assert not landing_credited(_v("landed in deadbeef12"), ["R1"], LONG)


def test_all_letter_hex_token_is_a_contrary_citation():
    assert not landing_credited(_v("landed in deadbeef"), ["R1"], LONG)
    assert not landing_credited(_v("b" * 10), ["R1"], LONG)


def test_all_letter_hex_token_matching_the_landing_credits():
    assert landing_credited(_v("f665c40f92aaaaaa"), ["R1"], LONG)
    assert landing_credited(_v("aaaaaaa"), ["R1"], "a" * 40)


def test_list_mixing_prose_and_matching_sha_credits():
    assert landing_credited(_v(["tests pass", "f665c40"]), ["R1"], LONG)


def test_other_rows_ignored():
    assert landing_credited(_v("deadbeef12"), ["R2"], LONG)

