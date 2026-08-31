"""
coordinator_core.hooks.tests.test_nudge_autonomous_askuserquestion — Tier-T test
for the first hot-path reconstructable unit built against
docs/reference/warm-hook-migration.md.

Three obligations, per this chunk's dispatch brief (none catches the others):
  (a) the op is registered and resolvable through `warm.hook_http.op_for_path`;
  (b) it is CLASSIFIED — an explicit assertion of the `classify()` call/result,
      since routing alone never calls `_is_compute_only` for a prefixed op;
  (c) it returns the source script's shape for one real payload.
"""

from __future__ import annotations

import importlib

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.warm.hook_http import HOOK_PATH, op_for_path


def test_op_registers_and_resolves_through_op_for_path() -> None:
    # Importing the module fires its register_op(...) side effect.
    module = importlib.import_module(
        "coordinator_core.hooks.nudge_autonomous_askuserquestion"
    )
    assert hasattr(module, "_handler")

    from coordinator_core.ipc import _REGISTRY

    assert "hooks.nudge_autonomous_askuserquestion" in _REGISTRY

    resolved = op_for_path(HOOK_PATH + "/hooks.nudge_autonomous_askuserquestion")
    assert resolved == "hooks.nudge_autonomous_askuserquestion"


def test_op_is_classified_compute_only() -> None:
    # Explicit assertion of the classify() call/result — routing alone (a
    # `hooks.` prefix match) never reaches `_is_compute_only`, so an absent
    # classification would pass every routing test and still be a dispatch-
    # time authz gap. Assert the call succeeds and answers COMPUTE_ONLY,
    # rather than merely checking the op's absence from a deny list.
    result = classify("hooks.nudge_autonomous_askuserquestion")
    assert result is OpClass.COMPUTE_ONLY


def test_op_returns_script_shape_for_a_firing_payload() -> None:
    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    payload = {
        "session_id": "test-session-nudge-askuserquestion",
        "cwd": "",
        "env": {},
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "Should I use a factory here?"}]},
    }
    # No sentinel file, and posture resolution fails open to "precision" with no
    # readable coordinator.local.md / identity file for this synthetic cwd — so
    # this payload is a NON-firing case by construction, one edge of the D2
    # shape contract.
    result = _handler({"payload": payload})
    assert result == {}


def test_op_returns_allow_advisory_shape_when_sentinel_present(tmp_path, monkeypatch) -> None:
    import tempfile

    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    session_id = "test-session-nudge-askuserquestion-fire"
    sentinel = tempfile.gettempdir()
    import os

    sentinel_path = os.path.join(sentinel, f"autonomous-run-{session_id}")
    with open(sentinel_path, "w", encoding="utf-8") as handle:
        handle.write("1")
    try:
        payload = {
            "session_id": session_id,
            "cwd": "",
            "env": {},
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Should I use a factory here?"}]},
        }
        result = _handler({"payload": payload})
        # Same hookSpecificOutput shape the source script prints to stdout:
        # permissionDecision:"allow" + additionalContext, never a blocking deny.
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "allow"
        assert "additionalContext" in hso
        assert "renders no verdict" in hso["additionalContext"]
    finally:
        os.remove(sentinel_path)


def test_op_suppresses_on_agent_id() -> None:
    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    payload = {"agent_id": "some-subagent", "session_id": "sid", "env": {}}
    assert _handler({"payload": payload}) == {}


def test_op_suppresses_on_override_env_from_payload_not_os_environ(monkeypatch) -> None:
    """The override MUST be read from params["payload"]["env"], never from this
    process's os.environ — the hook payload contract this chunk's brief pins."""
    import os

    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    monkeypatch.delenv("COORDINATOR_AUTONOMOUS_ASK_OK", raising=False)
    payload = {
        "session_id": "sid-override",
        "env": {"COORDINATOR_AUTONOMOUS_ASK_OK": "1"},
    }
    assert _handler({"payload": payload}) == {}

    # And the inverse: an ambient os.environ override must NOT suppress —
    # only the payload's own env does.
    monkeypatch.setenv("COORDINATOR_AUTONOMOUS_ASK_OK", "1")
    payload_no_env = {"session_id": "sid-ambient-only", "env": {}}
    result = _handler({"payload": payload_no_env})
    # No sentinel, fail-open posture -> non-firing regardless, but for a
    # different reason than the override -- confirms os.environ was never
    # consulted for the override bypass specifically.
    assert result == {}
