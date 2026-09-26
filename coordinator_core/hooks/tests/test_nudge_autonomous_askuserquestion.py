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
import os
import tempfile

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.warm.hook_http import HOOK_PATH, op_for_path


def test_op_registers_and_resolves_through_op_for_path() -> None:
    module = importlib.import_module(
        "coordinator_core.hooks.nudge_autonomous_askuserquestion"
    )
    assert hasattr(module, "_handler")

    from coordinator_core.ipc import _REGISTRY

    assert "hooks.nudge_autonomous_askuserquestion" in _REGISTRY

    resolved = op_for_path(HOOK_PATH + "/hooks.nudge_autonomous_askuserquestion")
    assert resolved == "hooks.nudge_autonomous_askuserquestion"


def test_op_is_classified_compute_only() -> None:
    # time authz gap. Assert the call succeeds and answers COMPUTE_ONLY,
    result = classify("hooks.nudge_autonomous_askuserquestion")
    assert result is OpClass.COMPUTE_ONLY


def test_op_suppresses_for_a_non_firing_payload() -> None:
    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    payload = {
        "session_id": "test-session-nudge-askuserquestion",
        "cwd": "",
        "env": {},
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "Should I use a factory here?"}]},
    }
    result = _handler({"payload": payload})
    assert result == {}


def test_op_returns_allow_advisory_shape_when_sentinel_present(tmp_path, monkeypatch) -> None:
    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    session_id = "test-session-nudge-askuserquestion-fire"
    sentinel = tempfile.gettempdir()

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


def test_op_suppresses_on_override_env_from_payload_not_os_environ(tmp_path, monkeypatch) -> None:
    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    monkeypatch.delenv("COORDINATOR_AUTONOMOUS_ASK_OK", raising=False)
    payload = {
        "session_id": "sid-override",
        "env": {"COORDINATOR_AUTONOMOUS_ASK_OK": "1"},
    }
    assert _handler({"payload": payload}) == {}

    session_id = "sid-ambient-only"
    sentinel_path = os.path.join(tempfile.gettempdir(), f"autonomous-run-{session_id}")
    with open(sentinel_path, "w", encoding="utf-8") as handle:
        handle.write("1")
    try:
        monkeypatch.setenv("COORDINATOR_AUTONOMOUS_ASK_OK", "1")
        payload_no_env = {"session_id": session_id, "env": {}}
        result = _handler({"payload": payload_no_env})
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    finally:
        os.remove(sentinel_path)


def _write_identity(base_dir, posture: str) -> None:
    identity_dir = os.path.join(str(base_dir), ".claude")
    os.makedirs(identity_dir, exist_ok=True)
    identity_path = os.path.join(identity_dir, "coordinator-identity.yaml")
    with open(identity_path, "w", encoding="utf-8") as handle:
        handle.write(f"engagement_posture: {posture}\n")


def test_claude_home_from_payload_env_is_read(tmp_path, monkeypatch) -> None:
    """Payload env CLAUDE_HOME points at an identity file reading "default", and
    the quarantined platform home has no identity file at all -- posture must
    still resolve through the payload's CLAUDE_HOME."""
    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    home_dir = tmp_path / "a"
    home_dir.mkdir()
    _write_identity(home_dir, "default")

    quarantined_home = tmp_path / "quarantined-platform-home"
    quarantined_home.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(quarantined_home))

    payload = {
        "session_id": "sid-claude-home-default",
        "cwd": "",
        "env": {"CLAUDE_HOME": str(home_dir)},
    }
    result = _handler({"payload": payload})
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "posture=default" in hso["additionalContext"]


def test_claude_home_wins_over_platform_home(tmp_path, monkeypatch) -> None:
    """CLAUDE_HOME reads "precision" while the quarantined platform home reads
    "default" -- CLAUDE_HOME must win, so the op stays inert ({})."""
    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    home_dir = tmp_path / "b"
    home_dir.mkdir()
    _write_identity(home_dir, "precision")

    quarantined_home = tmp_path / "quarantined-platform-home-2"
    quarantined_home.mkdir()
    _write_identity(quarantined_home, "default")
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(quarantined_home))

    payload = {
        "session_id": "sid-claude-home-wins",
        "cwd": "",
        "env": {"CLAUDE_HOME": str(home_dir)},
    }
    assert _handler({"payload": payload}) == {}


def test_empty_claude_home_falls_through_to_platform_home(tmp_path, monkeypatch) -> None:
    """CLAUDE_HOME is set but empty in the payload env -- that counts as unset,
    so the reader must fall through to the (quarantined) platform home, which
    reads "default"."""
    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    quarantined_home = tmp_path / "quarantined-platform-home-3"
    quarantined_home.mkdir()
    _write_identity(quarantined_home, "default")
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(quarantined_home))

    payload = {
        "session_id": "sid-claude-home-empty",
        "cwd": "",
        "env": {"CLAUDE_HOME": ""},
    }
    result = _handler({"payload": payload})
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "posture=default" in hso["additionalContext"]


def test_engine_process_os_environ_claude_home_is_never_read(tmp_path, monkeypatch) -> None:
    """The rung must read CLAUDE_HOME from the payload env, never from this
    process's own os.environ -- setting it there only, with no CLAUDE_HOME in
    the payload env, must not affect posture resolution at all."""
    from coordinator_core.hooks.nudge_autonomous_askuserquestion import _handler

    ambient_home = tmp_path / "ambient-os-environ-home"
    ambient_home.mkdir()
    _write_identity(ambient_home, "default")
    monkeypatch.setenv("CLAUDE_HOME", str(ambient_home))

    quarantined_home = tmp_path / "quarantined-platform-home-4"
    quarantined_home.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(quarantined_home))

    payload = {
        "session_id": "sid-claude-home-os-environ-isolation",
        "cwd": "",
        "env": {},
    }
    assert _handler({"payload": payload}) == {}
