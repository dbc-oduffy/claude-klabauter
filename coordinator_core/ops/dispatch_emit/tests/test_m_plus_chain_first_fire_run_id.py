"""An {ask} fire that sizes M+ chains into its plan run on the first fire.

The chained emit's reply travels scout/gate agent -> `_gate.chain_emit` -> the ask script's chain check, which
halts unless it reads a run id. The agent returns what the declared `chain_emit` schema names, so the real
`dispatch.emit` reply is projected through that schema before the script reads it.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_m_plus_chain_first_fire_run_id.py -q
"""

from __future__ import annotations

import pytest

from coordinator_core.hooks.tests.test_multi_ask_sibling_runs_e2e import _run_m_plus_block
from coordinator_core.ops.dispatch_emit import ask_compose, op
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put

_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_RUN_ID_REASON = "emit reply run_id is not a run id"


@pytest.fixture
def real_emit_reply(tmp_path, monkeypatch):
    """The reply `dispatch.emit` gives the chain-emit params of an M+ {ask} fire, from the real op."""
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    _put(tmp_path, tshirt="M", route="plan", accepted=None)
    return op._dispatch_emit(
        {"ask": True, "sizing_path": REL, "session_id": "s", "target_root": str(tmp_path), "launcher_child": True},
        repo_root=tmp_path,
    )


def _relayed(reply: dict, schema: dict) -> dict:
    """The reply as an agent bound to `schema` returns it: the declared properties, nothing else."""
    return {k: v for k, v in reply.items() if k in schema.get("properties", {})}


_RELAY_SCHEMAS = {
    "gate": ask_compose._GATE_SCHEMA["properties"]["chain_emit"],
    "scout": ask_compose._ASK_SCOUT_SCHEMA["properties"]["chain_emit"],
}


@pytest.mark.spawns_process
@pytest.mark.parametrize("leg", sorted(_RELAY_SCHEMAS))
def test_the_real_emit_reply_relayed_through_the_declared_schema_starts_the_chained_run(tmp_path, real_emit_reply, leg):
    assert isinstance(real_emit_reply["run_id"], str) and real_emit_reply["run_id"]
    got = _run_m_plus_block(tmp_path, _relayed(real_emit_reply, _RELAY_SCHEMAS[leg]))
    assert "halted" not in got["out"] or got["out"]["halted"] is None, got["out"]
    assert len(got["started"]) == 1 and got["started"][0].endswith(f"{real_emit_reply['run_id']}.workflow.mjs")
    assert got["out"]["done"] is True and got["out"]["chained_from"] == "run-1"


@pytest.mark.parametrize("leg", sorted(_RELAY_SCHEMAS))
def test_the_relay_schema_names_the_fields_the_chain_check_reads(leg):
    props = _RELAY_SCHEMAS[leg]["properties"]
    assert {"path", "run_id"} <= set(props)


@pytest.mark.spawns_process
def test_a_relay_schema_declaring_no_fields_halts_the_first_fire(tmp_path, real_emit_reply):
    """The cause: an undeclared `chain_emit` object projects to nothing, so the chain check reads no run id."""
    got = _run_m_plus_block(tmp_path, _relayed(real_emit_reply, {"type": ["object", "null"]}))
    assert got["started"] == []
    assert "the chained run did not start" in got["out"]["halted"]["reason"]


@pytest.mark.spawns_process
def test_a_genuine_non_start_still_halts_on_the_run_id_check(tmp_path, real_emit_reply):
    reply = {"path": real_emit_reply["path"], "run_id": ""}
    got = _run_m_plus_block(tmp_path, reply)
    assert got["started"] == []
    assert _RUN_ID_REASON in got["out"]["halted"]["reason"]
