"""dispatch.ask_gate stops a refire of a run already recorded landed and reports the sha, before any leg."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import ask_gate, halt_refire, halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REFUSAL, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "warp-2026-11-21-refire-20261121T120000-a1b2c3"
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def legs(repo, monkeypatch: pytest.MonkeyPatch) -> list:  # noqa: F811
    """Records each `gate` call: the leg a landed refire must never reach."""
    _git(repo, "init", "-q")
    calls: list = []
    real = ask_gate.gate

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(ask_gate, "gate", spy)
    return calls


def _assert_landed_stop(out: dict) -> None:
    assert out["arm"] is None and out["halt"]["kind"] == HALT_REFUSAL, out
    assert out["halt"]["landed_sha"] == SHA and SHA[:12] in out["halt"]["reason"]
    action = out["halt"]["next_action"]
    assert halt_refire.is_exit_next_action(action) and action["route"] == halt_refire.ROUTE_LANDED
    assert "manifest_handle" not in out


def test_a_run_whose_halt_marker_reads_landed_stops_with_its_sha(repo, legs):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED)
    record = repo / RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted="verify-leg-empty", sizing_rel=REL)
    assert halted_run.mark_landed(record, SHA)

    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo)

    _assert_landed_stop(out)
    assert legs == []


def test_a_run_whose_sibling_status_reads_landed_stops_with_its_sha(repo, legs):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED)
    status = repo / contract.status_rel(RUN)
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"status": contract.STATUS_LANDED, "sha": SHA}), encoding="utf-8")

    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo)

    _assert_landed_stop(out)
    assert legs == []


def test_a_halted_run_that_has_not_landed_still_reaches_the_gate(repo, legs):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED)
    halted_run.write_halt_marker(repo / RECORD_DIR_ROOT / RUN, halted="verify-leg-empty", sizing_rel=REL)

    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo)

    assert len(legs) == 1
    assert "landed_sha" not in (out.get("halt") or {}), out


def test_a_call_without_a_run_id_skips_the_landed_stop_and_reaches_the_gate(repo, legs):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED)

    out = ask_gate._handler({"sizing_path": REL}, repo)

    assert len(legs) == 1
    assert "landed_sha" not in (out.get("halt") or {}), out
