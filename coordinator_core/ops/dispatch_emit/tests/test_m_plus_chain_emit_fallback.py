"""A buildable gate arm (m_plus/roadmap, halt null) always reaches an engine-run dispatch.emit leg.

Why ask-20261106T125423-22d8c2 halted with "emit failed: not run: ...": the run was a launcher child. Its
M+/roadmap leg returned the `chain` hand-off with `...(_gate.chain_emit ?? {})` spread in, so the scout's
`{error: 'not run: ...'}` relay reached the launcher's `_chainHop`, which fails a hand-off carrying `error`
without emitting. The in-run (non-child) leg was never the cause: its single `_emitAgent` retry survives
`_share_chain_emit`/`_share_chain_check` and rescues a null or errored relay (pinned below). The child's
hand-off never carries `error`: a relay without a path is retried once by the child's own `chain-emit` agent,
and a failed retry is carried as `emit_error`, which leaves the launcher's `chain-emit` leg to run.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from coordinator_core.ops.dispatch_emit.tests import test_launcher_child_chain_carries_path as carry
from coordinator_core.ops.dispatch_emit.tests import test_launcher_child_m_plus_chains_incident as launcher
from coordinator_core.ops.dispatch_emit.tests import test_size_then_plan_e2e as e2e

pytestmark = pytest.mark.complete

_NOT_RUN = {
    "error": "not run: gate reply carried arm m_plus with null halt but dispatch.emit was not run by this scout"
}
_GOOD_PATH = "scratch/warp/warp-child.workflow.mjs"


def _drive_handoff(script, handoff, chain_emit):
    return launcher._drive(
        script,
        [{CHAIN_HANDOFF_KEY: handoff, "sizing": launcher._SIZING, "run_id": "ask-r1"}, {"route": "dispatch", "done": True}],
        chain_emit,
    )


@pytest.mark.spawns_process
@pytest.mark.parametrize("relay", [None, _NOT_RUN, {"error": "denied", "path": "", "run_id": ""}], ids=["null", "not-run", "denied"])
def test_launcher_child_hand_off_retries_a_missing_or_errored_relay_so_the_launcher_does_not_emit(tmp_path, relay):
    script = launcher._script(tmp_path)
    for leg in carry.handoff_legs():
        handoff, calls = carry.run_leg(leg, relay, launcher._good_chain(tmp_path))
        assert calls == 1 and "error" not in handoff and handoff["path"].endswith(".workflow.mjs")
        out = _drive_handoff(script, handoff, {"error": "must not be called"})
        assert out["r"]["status"] == "ok", out["r"]
        assert out["emits"] == []


@pytest.mark.spawns_process
@pytest.mark.parametrize("relay", [None, _NOT_RUN], ids=["null", "not-run"])
def test_launcher_child_hand_off_with_a_failed_retry_lets_the_launcher_emit(tmp_path, relay):
    script = launcher._script(tmp_path)
    for leg in carry.handoff_legs():
        handoff, calls = carry.run_leg(leg, relay, {"path": "", "run_id": "", "error": "denied"})
        assert calls == 1 and "error" not in handoff and "path" not in handoff and handoff["emit_error"]
        out = _drive_handoff(script, handoff, launcher._good_chain(tmp_path))
        assert out["r"]["status"] == "ok", out["r"]
        assert [e["label"] for e in out["emits"]] == ["chain-emit-ask-r1"]


@pytest.mark.spawns_process
def test_launcher_child_hand_off_drops_an_array_relay():
    for leg in carry.handoff_legs():
        handoff, _ = carry.run_leg(leg, ["a", "b"], None)
        assert "0" not in handoff and "1" not in handoff


@pytest.mark.spawns_process
def test_launcher_child_hand_off_keeps_a_valid_relay_so_the_launcher_does_not_emit_again(tmp_path):
    relay = launcher._good_chain(tmp_path)
    script = launcher._script(tmp_path)
    for leg in carry.handoff_legs():
        handoff, calls = carry.run_leg(leg, relay, {"error": "must not be called"})
        assert calls == 0
        assert handoff["path"] == relay["path"]
        out = launcher._drive(
            script,
            [{CHAIN_HANDOFF_KEY: handoff, "sizing": launcher._SIZING, "run_id": "ask-r1"}, {"route": "dispatch", "done": True}],
            {"error": "must not be called"},
        )
        assert out["r"]["status"] == "ok", out["r"]
        assert out["emits"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("relay", [None, _NOT_RUN], ids=["null", "not-run"])
def test_in_run_m_plus_leg_runs_the_engine_emit_agent_when_the_relay_is_missing_or_errored(tmp_path, monkeypatch, capsys, relay):
    scout, decision, verdict, _rel = e2e._fixture(tmp_path, monkeypatch, capsys, "M", "ceo", [])
    assert verdict["halt"] is None and verdict["arm"] == "m_plus", verdict
    stub = e2e._chain_stub(scout, decision, verdict, relay)
    stub["chain-emit"] = {"path": _GOOD_PATH, "run_id": "run-2"}
    out = e2e._run_script(e2e._script(), stub)
    assert "halted_by" not in out["result"], out["result"]
    assert out["seen"]["workflows"] == [e2e._CHILD_RESOLVED]
    assert out["seen"]["labels"] == ["size", "chain-emit"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_in_run_m_plus_leg_does_not_rerun_the_emit_when_the_relay_already_carries_a_valid_path(tmp_path, monkeypatch, capsys):
    scout, decision, verdict, _rel = e2e._fixture(tmp_path, monkeypatch, capsys, "M", "ceo", [])
    stub = e2e._chain_stub(scout, decision, verdict, {"path": _GOOD_PATH, "run_id": "run-2"})
    out = e2e._run_script(e2e._script(), stub)
    assert out["seen"]["labels"] == ["size"]
    assert out["seen"]["workflows"] == [e2e._CHILD_RESOLVED]
