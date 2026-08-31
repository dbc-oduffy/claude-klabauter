"""
coordinator_core.hooks.tests.test_runtime_tripwire_em_check — Tier-T test for
the PostToolUse(Agent) warm-door op (leg 2 of 2 of DoE-claude's
`runtime-tripwire-em-check.py`; the sibling UserPromptSubmit leg is out of
scope — Terminal, see the module's own docstring).

Three obligations, per this chunk's dispatch brief / the migration runbook
(none catches the others):
  (a) the op is registered and resolvable through `warm.hook_http.op_for_path`;
  (b) it is CLASSIFIED — an explicit assertion of the `classify()` call/result,
      since routing alone never calls `_is_compute_only` for a prefixed op;
  (c) it returns the source script's shape for one real, firing payload.

Plus behavior coverage over the subagent-detect gate and the two ported
advisory legs (push-failure log growth, hooks.json staleness).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.warm.hook_http import HOOK_PATH, op_for_path


def test_op_registers_and_resolves_through_op_for_path() -> None:
    module = importlib.import_module("coordinator_core.hooks.runtime_tripwire_em_check")
    assert hasattr(module, "_handler")

    from coordinator_core.ipc import _REGISTRY

    assert "hooks.runtime_tripwire_em_check" in _REGISTRY

    resolved = op_for_path(HOOK_PATH + "/hooks.runtime_tripwire_em_check")
    assert resolved == "hooks.runtime_tripwire_em_check"


def test_op_is_classified_mutating() -> None:
    result = classify("hooks.runtime_tripwire_em_check")
    assert result is OpClass.MUTATING


def _init_repo(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    (path / "coordinator" / "hooks").mkdir(parents=True)
    (path / "coordinator" / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")


def _base_payload(cwd: str, session_id: str = "sess-abc12345", agent_id: str = "") -> dict:
    return {
        "session_id": session_id,
        "agent_id": agent_id,
        "cwd": cwd,
    }


def test_op_suppresses_for_a_subagent_session(tmp_path) -> None:
    from coordinator_core.hooks.runtime_tripwire_em_check import _handler

    repo = tmp_path / "repo"
    _init_repo(repo)

    agents_dir = repo / ".git" / "coordinator-sessions" / ".agents" / "sess-abc12345"
    agents_dir.mkdir(parents=True)
    (agents_dir / "em-session-id.txt").write_text("em-session\n", encoding="utf-8")

    payload = _base_payload(str(repo))
    result = _handler({"payload": payload})
    assert result == {}


def test_op_returns_no_advisory_on_first_call_establishing_baselines(tmp_path) -> None:
    """First call this session records both cursors' baselines and never
    alarms — mirrors the source script's own first-check-records-baseline
    contract for both legs."""
    from coordinator_core.hooks.runtime_tripwire_em_check import _handler

    repo = tmp_path / "repo"
    _init_repo(repo)

    payload = _base_payload(str(repo))
    result = _handler({"payload": payload})
    assert result == {}

    cursor_dir = repo / ".git" / "coordinator-sessions" / "sess-abc12345"
    assert (cursor_dir / "hooks-json-boot-hash.txt").is_file()


def test_op_surfaces_hooks_json_staleness_on_second_call_after_edit(tmp_path) -> None:
    from coordinator_core.hooks.runtime_tripwire_em_check import _handler

    repo = tmp_path / "repo"
    _init_repo(repo)

    payload = _base_payload(str(repo))
    first = _handler({"payload": payload})
    assert first == {}

    (repo / "coordinator" / "hooks" / "hooks.json").write_text(
        '{"changed": true}', encoding="utf-8"
    )

    second = _handler({"payload": payload})
    hso = second["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "PLUGIN-HOOKS-JSON-RESTART-GATED" in hso["additionalContext"]


def test_op_surfaces_push_failure_growth_on_a_work_branch(tmp_path) -> None:
    from coordinator_core.hooks.runtime_tripwire_em_check import _handler

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "state").mkdir()

    # HEAD on a work/* branch, no upstream configured (push_failure_verdict
    # degrades to "indeterminate" -- still a firing verdict).
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/work/2026-08-31\n", encoding="utf-8")

    payload = _base_payload(str(repo))
    first = _handler({"payload": payload})
    assert first == {}  # baseline call: log absent -- nothing to establish yet

    (repo / ".git" / "push-failures.log").write_text(
        "[2026-08-31T00:00:00Z] PUSH FAILED on work/2026-08-31\n", encoding="utf-8"
    )

    second = _handler({"payload": payload})
    assert second == {}  # this call's own first sight of the log establishes ITS baseline

    with open(repo / ".git" / "push-failures.log", "a", encoding="utf-8") as fh:
        fh.write("[2026-08-31T00:05:00Z] PUSH FAILED on work/2026-08-31\n")

    third = _handler({"payload": payload})
    hso = third["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert (
        "MID-SESSION FAILURE" in hso["additionalContext"]
        or "mid-session note" in hso["additionalContext"]
    )


def test_op_returns_no_advisory_on_malformed_payload() -> None:
    from coordinator_core.hooks.runtime_tripwire_em_check import _handler

    assert _handler({"payload": "not-a-mapping"}) == {}
    assert _handler({}) == {}


def test_op_returns_no_advisory_without_session_id(tmp_path) -> None:
    from coordinator_core.hooks.runtime_tripwire_em_check import _handler

    repo = tmp_path / "repo"
    _init_repo(repo)
    result = _handler({"payload": {"cwd": str(repo)}})
    assert result == {}
