"""A buildable gate arm (m_plus/roadmap, halt null) always reaches an engine-run dispatch.emit leg.

Why ask-20261106T125423-22d8c2 halted with "emit failed: not run: ...": the run was a launcher child. Its
M+/roadmap leg returned the `chain` hand-off with `...(_gate.chain_emit ?? {})` spread in, so the scout's
`{error: 'not run: ...'}` relay reached the launcher's `_chainHop`, which fails a hand-off carrying `error`
without emitting. The in-run (non-child) leg was never the cause: its single `_emitAgent` retry survives
`_share_chain_emit`/`_share_chain_check` and rescues a null or errored relay (pinned below). The fix drops an
errored or absent relay from the hand-off, so the launcher's own `chain-emit` leg runs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests import test_launcher_child_m_plus_chains_incident as launcher
from coordinator_core.ops.dispatch_emit.tests import test_size_then_plan_e2e as e2e

_NOT_RUN = {
    "error": "not run: gate reply carried arm m_plus with null halt but dispatch.emit was not run by this scout"
}
_GOOD_PATH = "scratch/warp/warp-child.workflow.mjs"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    return node


def _handoff_legs() -> list[str]:
    script = compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: ("", []), plan_blitz_text="stub", launcher_child=True, **REVIEW_KW,
    )
    legs = [
        leg for leg in re.findall(r"return \{ chain: .*?, sizing: _sizingRel, run_id: _runId \};", script)
        if "_gate.chain_emit" in leg
    ]
    assert legs, "launcher child composed no chain hand-off"
    return legs


def _handoff(leg: str, chain_emit) -> dict:
    code = (
        "const _sizingRel = 'state/sizings/s.yaml', _SESSION_ID = 's', REPO_ROOT = '/r', _runId = 'ask-r1';\n"
        f"const _crossRepoApproved = false; const _gate = {{ arm: 'm_plus', chain_emit: {json.dumps(chain_emit)} }};\n"
        f"console.log(JSON.stringify((() => {{ {leg} }})()));\n"
    )
    out = subprocess.run([_node(), "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)[CHAIN_HANDOFF_KEY]


@pytest.mark.spawns_process
@pytest.mark.parametrize("relay", [None, _NOT_RUN, {"error": "denied", "path": "", "run_id": ""}], ids=["null", "not-run", "denied"])
def test_launcher_child_hand_off_drops_a_missing_or_errored_relay_and_the_launcher_emits(tmp_path, relay):
    script = launcher._script(tmp_path)
    for leg in _handoff_legs():
        handoff = _handoff(leg, relay)
        assert "error" not in handoff and "path" not in handoff
        out = launcher._drive(
            script,
            [{CHAIN_HANDOFF_KEY: handoff, "sizing": launcher._SIZING, "run_id": "ask-r1"}, {"route": "dispatch", "done": True}],
            launcher._good_chain(tmp_path),
        )
        assert out["r"]["status"] == "ok", out["r"]
        assert [e["label"] for e in out["emits"]] == ["chain-emit-ask-r1"]


@pytest.mark.spawns_process
def test_launcher_child_hand_off_drops_an_array_relay():
    for leg in _handoff_legs():
        handoff = _handoff(leg, ["a", "b"])
        assert "0" not in handoff and "1" not in handoff


@pytest.mark.spawns_process
def test_launcher_child_hand_off_keeps_a_valid_relay_so_the_launcher_does_not_emit_again(tmp_path):
    relay = launcher._good_chain(tmp_path)
    script = launcher._script(tmp_path)
    for leg in _handoff_legs():
        handoff = _handoff(leg, relay)
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
