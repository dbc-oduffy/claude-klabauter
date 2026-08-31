"""
coordinator_core.hooks.tests.test_watchdog_undischarged_next_move — Tier-T test
for the watchdog-undischarged-next-move port (chunk C4,
docs/reference/warm-hook-migration.md).

Three obligations, per this chunk's dispatch brief (none catches the others):
  (a) the op is registered and resolvable through `warm.hook_http.op_for_path`;
  (b) it is CLASSIFIED — an explicit assertion of the `classify()` call/result;
  (c) it returns the source script's shape for one real, firing payload, on
      BOTH legs (PostToolUse emission, Stop report).
"""

from __future__ import annotations

import importlib
import json
import os

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.warm.hook_http import HOOK_PATH, op_for_path

_OP_NAME = "hooks.watchdog_undischarged_next_move"


def _make_repo(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    return str(repo_root)


def _ledger_path(repo_root: str, session_id: str) -> str:
    return os.path.join(repo_root, "state", "subagent-share", session_id, "next-move-ledger.jsonl")


def test_op_registers_and_resolves_through_op_for_path() -> None:
    module = importlib.import_module(
        "coordinator_core.hooks.watchdog_undischarged_next_move"
    )
    assert hasattr(module, "_handler")

    from coordinator_core.ipc import _REGISTRY

    assert _OP_NAME in _REGISTRY

    resolved = op_for_path(HOOK_PATH + "/" + _OP_NAME)
    assert resolved == _OP_NAME


def test_op_is_classified_mutating() -> None:
    # Explicit assertion of the classify() call/result — routing alone (a
    # `hooks.` prefix match) never reaches `_is_compute_only`, so an absent
    # classification would pass every routing test and still be a
    # dispatch-time authz gap.
    assert classify(_OP_NAME) is OpClass.MUTATING


def test_post_tool_use_leg_is_always_silent(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    payload = {
        "session_id": "sid-pickup",
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:pickup"},
    }
    result = _handler({"payload": payload})
    assert result == {}

    # But the ledger side-effect landed — the PostToolUse leg is silent
    # bookkeeping, not a no-op.
    ledger = _ledger_path(repo_root, "sid-pickup")
    assert os.path.isfile(ledger)
    with open(ledger, "r", encoding="utf-8") as fh:
        record = json.loads(fh.readline())
    assert record["seam"] == "pickup->next-move"
    assert record["discharged_at"] is None
    assert record["fired"] is False


def test_stop_leg_reports_undischarged_obligation_at_precision(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    session_id = "sid-stop-fire"

    open_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:pickup"},
    }
    assert _handler({"payload": open_payload}) == {}

    # No coordinator.local.md / identity file resolvable for this synthetic
    # cwd -> posture fails open to "precision" -> non-blocking allow_advisory,
    # the same hookSpecificOutput shape the source script prints to stdout at
    # posture "precision".
    stop_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    result = _handler({"payload": stop_payload})
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "Stop"
    assert hso["permissionDecision"] == "allow"
    assert "Skill|Agent(the narrated next move)" in hso["additionalContext"]

    # One-fire-per-obligation latch: a second Stop for the same undischarged
    # obligation must stay silent.
    assert _handler({"payload": stop_payload}) == {}


def test_stop_leg_blocks_at_default_posture(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    with open(os.path.join(repo_root, "coordinator.local.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nengagement_posture: default\n---\n")

    session_id = "sid-stop-block"
    open_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:pickup"},
    }
    assert _handler({"payload": open_payload}) == {}

    stop_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    result = _handler({"payload": stop_payload})
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "Stop"
    assert hso["permissionDecision"] == "deny"
    assert "Invoke it now" in hso["permissionDecisionReason"]


def test_discharge_closes_the_obligation_before_stop_fires(tmp_path) -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    repo_root = _make_repo(tmp_path)
    session_id = "sid-discharge"

    open_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:review"},
    }
    assert _handler({"payload": open_payload}) == {}

    # review-a1-a2 discharges on ANY subsequent Agent call.
    discharge_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "tool_name": "Agent",
        "tool_input": {},
    }
    assert _handler({"payload": discharge_payload}) == {}

    stop_payload = {
        "session_id": session_id,
        "cwd": repo_root,
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    assert _handler({"payload": stop_payload}) == {}


def test_post_tool_use_suppresses_on_agent_id() -> None:
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    payload = {"agent_id": "some-subagent", "session_id": "sid", "tool_name": "Skill"}
    assert _handler({"payload": payload}) == {}


def test_session_id_is_read_from_payload_never_from_environment(tmp_path, monkeypatch) -> None:
    """The ledger must be keyed on params["payload"]["session_id"], never on
    this process's own environment (trap 1 in the module docstring)."""
    from coordinator_core.hooks.watchdog_undischarged_next_move import _handler

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "wrong-session-from-env")
    repo_root = _make_repo(tmp_path)
    payload = {
        "session_id": "sid-from-payload",
        "cwd": repo_root,
        "tool_name": "Skill",
        "tool_input": {"skill": "coordinator:pickup"},
    }
    assert _handler({"payload": payload}) == {}
    assert os.path.isfile(_ledger_path(repo_root, "sid-from-payload"))
    assert not os.path.isfile(_ledger_path(repo_root, "wrong-session-from-env"))
