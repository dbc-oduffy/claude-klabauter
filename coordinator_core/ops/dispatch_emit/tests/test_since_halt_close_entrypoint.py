"""Real-entrypoint pins for ``halt_landing.since_halt_close`` over the real unchanged-since-halt reason."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
from coordinator_core.ops.dispatch_emit.halt_landing import since_halt_close
from coordinator_core.ops.dispatch_emit.pre_halt_ancestry import CITED_DETAIL_KEY

SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
FAIL = [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def _verdict(met_by: str) -> dict:
    return {"items": [{"id": "R1", "status": "met", "met_by": met_by}]}


def test_prose_met_by_returns_landing_sha():
    got = since_halt_close({"landed_sha": SHA}, FAIL, _verdict("Incident test and write_back refusal pass"))
    assert got == SHA


def test_matching_short_sha_returns_landing_sha():
    assert since_halt_close({"landed_sha": SHA}, FAIL, _verdict(f"landed in {SHA[:10]}")) == SHA


def test_id_in_cited_detail_returns_landing_sha_despite_contrary_token():
    since_halt = {"landed_sha": SHA, CITED_DETAIL_KEY: {"R1": "f" * 40}}
    assert since_halt_close(since_halt, FAIL, _verdict("deadbeef")) == SHA


def test_unrelated_all_letter_hex_token_refuses():
    assert since_halt_close({"landed_sha": SHA}, FAIL, _verdict("see deadbeef")) is None


def test_non_since_halt_failure_reason_refuses():
    failures = FAIL + [{"id": "R2", "reason": "some other refusal"}]
    assert since_halt_close({"landed_sha": SHA}, failures, _verdict("prose")) is None


def test_empty_failures_refuses():
    assert since_halt_close({"landed_sha": SHA}, [], _verdict("prose")) is None


def test_missing_landed_sha_refuses():
    assert since_halt_close({}, FAIL, _verdict("prose")) is None
    assert since_halt_close({"landed_sha": ""}, FAIL, _verdict("prose")) is None
