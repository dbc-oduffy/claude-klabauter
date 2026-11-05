"""Two callers share one warm engine; each minted run-stage.json carries its own caller's session."""

import pytest

from coordinator_core.hooks import submit_engine_ask as hook
from coordinator_core.ops.dispatch_emit import ask_contract as c
from coordinator_core.ops.dispatch_emit import requirements_ledger
from coordinator_core.session.core import session_identity_override, warm_served_request

SPAWNER = "e9b562a0-383c-447f-a14b-c9d9fcd9284f"
CALLER_A = "11111111-1111-4111-8111-111111111111"
CALLER_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _spawner_env(monkeypatch):
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SPAWNER)


def _minted(root, run_id):
    stage = c.read_run_stage(root, run_id)
    assert stage["stage"] == "minted"
    return stage["session_id"]


def test_ledger_start_carried_session_wins_over_spawner_env(tmp_path):
    with warm_served_request():
        with session_identity_override(CALLER_A):
            requirements_ledger.start(tmp_path, "run-a")
        with session_identity_override(CALLER_B):
            requirements_ledger.start(tmp_path, "run-b")
    assert _minted(tmp_path, "run-a") == CALLER_A
    assert _minted(tmp_path, "run-b") == CALLER_B


def test_warm_request_without_a_carried_session_records_none_not_the_spawner(tmp_path):
    with warm_served_request():
        requirements_ledger.start(tmp_path, "run-x")
    assert _minted(tmp_path, "run-x") is None


def test_cold_start_still_reads_the_process_environment(tmp_path):
    requirements_ledger.start(tmp_path, "run-cold")
    assert _minted(tmp_path, "run-cold") == SPAWNER


def test_explicit_session_beats_ambient(tmp_path):
    with warm_served_request():
        requirements_ledger.start(tmp_path, "run-e", session_id=CALLER_B)
    assert _minted(tmp_path, "run-e") == CALLER_B


def test_hook_mint_path_records_each_callers_payload_session(tmp_path):
    def emit(params, root):
        requirements_ledger.start(root, params["run"])
        return {}

    with warm_served_request():
        hook._emit_within_budget(emit, {"run": "hook-a", "session_id": CALLER_A}, tmp_path, scan=False)
        hook._emit_within_budget(emit, {"run": "hook-b", "session_id": CALLER_B}, tmp_path, scan=False)
        hook._emit_within_budget(emit, {"run": "hook-n"}, tmp_path, scan=False)
    assert _minted(tmp_path, "hook-a") == CALLER_A
    assert _minted(tmp_path, "hook-b") == CALLER_B
    assert _minted(tmp_path, "hook-n") is None
