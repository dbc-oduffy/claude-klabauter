"""
coordinator_core.tests.test_hooks_roundtrip — Round-trip contract tests for the 7
advisory hook ops and the _envelope builders.

Tests verify D2 envelope shapes (allow_advisory / context_only / no_advisory /
post_advisory / deny) for each op's representative paths and suppression conditions.

All handlers are async; we use asyncio.run() in plain sync test functions to avoid
the pytest-asyncio dependency (engine is stdlib-only; prefer no test-infra additions).

GOLDEN normalization: additionalContext embeds volatile text; assertions use ``in``
/ substring checks rather than byte-exact matching.

Spec backlink: docs/plans/2026-07-04-pcore-04-advisory-hook-ops-claude-klabauter-engine.md § C8
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from coordinator_core import _hook_envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(result):
    """Run an async coroutine synchronously (no pytest-asyncio needed), or pass a
    plain (already-computed) result straight through — some handlers are `async def`
    and some are plain `def` (2026-08-07 zero-await conversions; a plain-`def`
    handler's return value is already a dict by the time it reaches this helper,
    never a coroutine)."""
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _unlink_if_exists(path: str) -> None:
    """Best-effort cleanup for a durable per-session state/sentinel file written
    by postuse_advisory_dispatch — these are test-scoped session ids, so this
    is scoped test hygiene, not a claim about the handler's own cleanup."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _hso(result: dict) -> dict:
    """Extract hookSpecificOutput from a non-empty result dict (fails loud if absent)."""
    assert "hookSpecificOutput" in result, f"hookSpecificOutput missing from: {result!r}"
    return result["hookSpecificOutput"]


def _assert_deny(result: dict, event_name: str = "PreToolUse") -> None:
    """Assert a deny D2 envelope (permissionDecision:'deny')."""
    hso = _hso(result)
    assert hso.get("hookEventName") == event_name, hso
    assert hso.get("permissionDecision") == "deny", hso
    assert "permissionDecisionReason" in hso, hso


def _assert_allow_advisory(result: dict, event_name: str = "PreToolUse") -> None:
    """Assert an allow_advisory D2 envelope."""
    hso = _hso(result)
    assert hso.get("hookEventName") == event_name, hso
    assert hso.get("permissionDecision") == "allow", hso
    assert "additionalContext" in hso, hso


def _assert_no_unlock_mechanism(text: str, session_id: str = "") -> None:
    """Assert `text` hands a subagent audience no pasteable unlock recipe.

    docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-keys.md AC-2/AC-3:
    no key, path, command, env var, sentinel name, CLI invocation, or doc pointer.
    """
    assert "touch" not in text, text
    assert ".foreground-ok" not in text, text
    assert ".git/coordinator-sessions" not in text, text
    assert "guard-override-keys" not in text, text
    if session_id:
        assert session_id not in text, text


def _assert_context_only(result: dict, event_name: str = "PreToolUse") -> None:
    """Assert a context_only D2 envelope (no permissionDecision key)."""
    hso = _hso(result)
    assert hso.get("hookEventName") == event_name, hso
    assert "additionalContext" in hso, hso
    assert "permissionDecision" not in hso, f"Unexpected permissionDecision in context_only: {hso!r}"


def _assert_post_advisory(result: dict) -> None:
    """Assert a post_advisory D2 envelope (PostToolUse hookEventName)."""
    hso = _hso(result)
    assert hso.get("hookEventName") == "PostToolUse", hso
    assert "additionalContext" in hso, hso


# ---------------------------------------------------------------------------
# STATIC: envelope builder shapes
# ---------------------------------------------------------------------------

def test_envelope_all_five_builders_exist() -> None:
    """_envelope exposes all 5 shape builders and each returns the correct D2 shape."""
    from coordinator_core.hooks._envelope import (
        allow_advisory,
        context_only,
        deny,
        no_advisory,
        post_advisory,
    )

    # Every agent-facing string these builders emit carries the provenance
    # marker — see _hook_envelope.COORDINATOR_PROVENANCE_MARKER for why (an
    # unmarked imperative in tool output is the signal an agent should refuse,
    # so coordinator's own traffic must be identifiable AS coordinator's).
    mark = _hook_envelope.COORDINATOR_PROVENANCE_MARKER

    # (a) allow_advisory
    r = allow_advisory("PreToolUse", "advisory text")
    hso = _hso(r)
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    assert hso["additionalContext"] == "%s advisory text" % mark

    # (b) context_only — no permissionDecision
    r = context_only("PreToolUse", "context text")
    hso = _hso(r)
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["additionalContext"] == "%s context text" % mark
    assert "permissionDecision" not in hso

    # (c) no_advisory — empty dict
    r = no_advisory()
    assert r == {}

    # (d) post_advisory — PostToolUse hookEventName
    r = post_advisory("post text")
    hso = _hso(r)
    assert hso["hookEventName"] == "PostToolUse"
    assert hso["additionalContext"] == "%s post text" % mark
    assert "permissionDecision" not in hso

    # (e) deny — permissionDecision:deny + permissionDecisionReason
    r = deny("PreToolUse", "reason text")
    hso = _hso(r)
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == "%s reason text" % mark


def test_envelope_provenance_marker_is_idempotent() -> None:
    """Stamping never double-prefixes, and never fabricates a bare-marker message.

    Idempotence matters because a caller may pass text that already came from
    another stamped builder; a doubled marker would read as a distinct, odder
    shape than the one agents are taught to recognise. The empty-string leg
    guards `rewrite_input`, which omits an empty context from its envelope
    entirely — stamping "" would convert that deliberate omission into an
    advisory carrying nothing but the marker.
    """
    mark = _hook_envelope.COORDINATOR_PROVENANCE_MARKER
    already = "%s already stamped" % mark
    assert _hook_envelope._stamp(already) == already
    assert _hook_envelope._stamp("") == ""


def test_envelope_deny_event_name_propagates() -> None:
    """deny() propagates the supplied event_name into hookEventName."""
    from coordinator_core.hooks._envelope import deny
    r = deny("PostToolUse", "late deny")
    assert _hso(r)["hookEventName"] == "PostToolUse"


def test_envelope_no_advisory_is_empty_dict() -> None:
    """no_advisory() returns exactly {} (spike-verified clean no-advisory)."""
    from coordinator_core.hooks._envelope import no_advisory
    assert no_advisory() == {}


# ---------------------------------------------------------------------------
# REGISTRY: all 6 hooks.* ops registered after import
# ---------------------------------------------------------------------------

def test_registry_enumeration_all_five_hooks() -> None:
    """After `import coordinator_core.ops`, all 5 hooks.* methods are in the registry."""
    import coordinator_core.ops  # noqa: F401 — triggers hooks registration side-effect
    from coordinator_core.ipc import _REGISTRY

    expected = {
        "hooks.nudge_foreground_agent_dispatch",
        "hooks.suggest_sonnet_research",
        "hooks.nudge_em_code_dispatch",
        "hooks.nudge_unauthorized_handoff",
        "hooks.postuse_advisory_dispatch",
    }
    for name in expected:
        assert name in _REGISTRY, f"Op not registered: {name!r}. Registered: {sorted(_REGISTRY)}"


# ---------------------------------------------------------------------------
# C1 — nudge_foreground_agent_dispatch
# ---------------------------------------------------------------------------

def test_foreground_dispatch_non_agent_tool_passes() -> None:
    """tool_name != 'Agent' → no_advisory (gate only fires on Agent dispatches)."""
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler
    assert _run(_handler({"tool_name": "Write"})) == {}


def test_foreground_dispatch_tool_absent_passes() -> None:
    """tool_name absent → no_advisory (mcp_tool "" == absent)."""
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler
    assert _run(_handler({})) == {}


def test_foreground_dispatch_background_true_passes() -> None:
    """run_in_background == 'true' → no_advisory (correct shape; background already set)."""
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler
    assert _run(_handler(
        {"tool_name": "Agent", "run_in_background": "true"}
    )) == {}


def test_foreground_dispatch_background_false_without_tool_input_denies() -> None:
    """present-and-false + no forwardable tool_input → deny fallback (D8).

    Nothing to rewrite means no correct call to emit, so the gate degrades to the
    historical bounce-back rather than passing the foreground dispatch through.
    """
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler
    # session_id="" so git-root resolution is skipped (no sentinel I/O in tests)
    result = _run(_handler(
        {"tool_name": "Agent", "run_in_background": "false", "session_id": ""}
    ))
    _assert_deny(result, "PreToolUse")
    assert "run_in_background: true" in _hso(result)["permissionDecisionReason"]


def test_foreground_dispatch_empty_tool_input_denies() -> None:
    """An EMPTY tool_input dict is not a rewrite target — it would erase the real args."""
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler
    result = _run(_handler(
        {"tool_name": "Agent", "run_in_background": "false", "session_id": "", "tool_input": {}}
    ))
    _assert_deny(result, "PreToolUse")


# Review: code-reviewer — Finding 1: non-empty tool_input missing `prompt` is not a safe
# rewrite target (updatedInput REPLACES the whole argument object); must fall back to deny
# rather than dispatch a subagent with no instructions.
def test_foreground_dispatch_tool_input_missing_prompt_denies() -> None:
    """Non-empty tool_input lacking `prompt` → deny fallback, not a corrupted rewrite."""
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler
    result = _run(_handler(
        {
            "tool_name": "Agent",
            "run_in_background": "false",
            "session_id": "",
            "tool_input": {"description": "no prompt here", "subagent_type": "coordinator:enricher"},
        }
    ))
    _assert_deny(result, "PreToolUse")


def test_foreground_dispatch_reroutes_to_background() -> None:
    """present-and-false + tool_input → updatedInput rewrite, all other keys preserved."""
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler
    result = _run(_handler({
        "tool_name": "Agent",
        "run_in_background": "false",
        "session_id": "",
        "tool_input": {
            "description": "check prior art",
            "prompt": "go",
            "subagent_type": "coordinator:prior-art-checker",
            "run_in_background": False,
        },
    }))
    hso = _hso(result)
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso, "rewrite is orthogonal to allow/deny"
    assert hso["updatedInput"] == {
        "description": "check prior art",
        "prompt": "go",
        "subagent_type": "coordinator:prior-art-checker",
        "run_in_background": True,
    }


def test_foreground_reroute_notice_fires_on_every_reroute(tmp_path) -> None:
    """The escape-hatch advisory rides EVERY reroute of a session, never just the first.

    Exercised through the real marker file (not module state) because each PreToolUse
    fire is a fresh process — the property under test is cross-process (D9, and the
    standing NOTICE ON EVERY REROUTE rule the 2026-07-30 revert left behind).
    """
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler

    git_root = tmp_path / ".git"
    git_root.mkdir()
    sid = "test-reroute-notice-every-01"
    params = {
        "tool_name": "Agent",
        "run_in_background": "false",
        "session_id": sid,
        "tool_input": {"prompt": "go"},
    }

    first = _run(_handler(dict(params), repo_root=str(git_root)))
    first_ctx = _hso(first)["additionalContext"]
    assert "AUTO-REROUTED" in first_ctx
    # EM audience (no agent_id) — the doc-pointer form of the override note is
    # permitted (plan AC-2), but the mechanism itself must still never render.
    assert "touch" not in first_ctx, first_ctx
    assert ".foreground-ok" not in first_ctx, first_ctx
    assert ".git/coordinator-sessions" not in first_ctx, first_ctx
    assert sid not in first_ctx, "session id must never render, EM audience included"
    assert not (git_root / "coordinator-sessions" / sid / ".foreground-reroute-noticed").exists(), (
        "no notice-once marker is ever written any more"
    )

    second = _run(_handler(dict(params), repo_root=str(git_root)))
    assert "AUTO-REROUTED" in _hso(second).get("additionalContext", ""), (
        "the notice must repeat on the second reroute — no bark-once"
    )
    assert _hso(second)["updatedInput"]["run_in_background"] is True, "reroute still applies"


def test_calibration_survives_a_fresh_process(tmp_path) -> None:
    """D7b: an absent-key dispatch acts on calibration written by an EARLIER process.

    The regression this pins: calibration used to live only in a module-level set, on the
    premise of a resident engine. DR-215 made every fire a fresh interpreter, so the set was
    always empty and the absent-key leg silently always passed. The in-memory set is cleared
    here to stand in for that process boundary — if only the set carried calibration, the
    second call would pass instead of acting.
    """
    import coordinator_core.hooks.nudge_foreground_agent_dispatch as mod
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler

    git_root = tmp_path / ".git"
    git_root.mkdir()
    sid = "test-durable-calibration-01"

    # Process 1: a present-key dispatch calibrates the session.
    assert _run(_handler(
        {"tool_name": "Agent", "run_in_background": "true", "session_id": sid},
        repo_root=str(git_root),
    )) == {}
    assert (git_root / "coordinator-sessions" / sid / ".harness-bg-capable").exists()

    # Process 2: fresh interpreter — in-memory calibration is gone, marker is not.
    mod._BG_CAPABLE_SESSIONS.discard(sid)
    result = _run(_handler(
        {"tool_name": "Agent", "session_id": sid, "tool_input": {"prompt": "go"}},
        repo_root=str(git_root),
    ))
    assert _hso(result)["updatedInput"]["run_in_background"] is True, (
        "calibrated absent-key dispatch must be acted on, not passed"
    )


def test_present_and_false_calibrates_for_later_absent_call(tmp_path) -> None:
    """D7b: a present-and-false call must durably calibrate too, not just present-and-true.

    Review Finding 1 (2026-07-31): the durable marker was written only inside the bg_true
    branch, so a session whose first Agent call was run_in_background=false got correctly
    rerouted but never wrote the marker. A later absent-key call in the same (spawn-per-call
    fresh) process then read uncalibrated and silently passed a foreground dispatch through
    unrewritten — the exact failure this gate exists to prevent.
    """
    import coordinator_core.hooks.nudge_foreground_agent_dispatch as mod
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler

    git_root = tmp_path / ".git"
    git_root.mkdir()
    sid = "test-present-false-calibration-01"

    # Call 1: present-and-false — must reroute AND durably calibrate.
    result1 = _run(_handler(
        {"tool_name": "Agent", "run_in_background": "false", "session_id": sid,
         "tool_input": {"prompt": "go"}},
        repo_root=str(git_root),
    ))
    assert _hso(result1)["updatedInput"]["run_in_background"] is True, (
        "present-and-false must reroute"
    )
    assert (git_root / "coordinator-sessions" / sid / ".harness-bg-capable").exists(), (
        "presence (even false) must write the durable calibration marker"
    )

    # Simulate the next dispatch as a fresh spawn-per-call process: in-memory set is gone,
    # only the durable marker can carry calibration forward.
    mod._BG_CAPABLE_SESSIONS.discard(sid)

    # Call 2: absent-key, same session — must be acted on (rerouted), not silently passed.
    result2 = _run(_handler(
        {"tool_name": "Agent", "session_id": sid, "tool_input": {"prompt": "go again"}},
        repo_root=str(git_root),
    ))
    assert _hso(result2)["updatedInput"]["run_in_background"] is True, (
        "calibrated-via-false absent-key dispatch must be acted on, not passed silently"
    )


def test_uncalibrated_absent_key_still_passes(tmp_path) -> None:
    """Brick-proofing intact: with no calibration marker, an absent key passes untouched.

    On a build that never exposes run_in_background, EVERY dispatch omits it — acting on
    that would gate every Agent call on the machine.
    """
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler

    git_root = tmp_path / ".git"
    git_root.mkdir()
    result = _run(_handler(
        {"tool_name": "Agent", "session_id": "test-uncalibrated-01",
         "tool_input": {"prompt": "go"}},
        repo_root=str(git_root),
    ))
    assert result == {}


def test_foreground_escape_hatch_passes_through_unrewritten(tmp_path) -> None:
    """.foreground-ok → the dispatch runs foreground as written; no rewrite, no notice."""
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler

    git_root = tmp_path / ".git"
    sid = "test-reroute-hatch-01"
    hatch = git_root / "coordinator-sessions" / sid / ".foreground-ok"
    hatch.parent.mkdir(parents=True)
    hatch.touch()

    result = _run(_handler({
        "tool_name": "Agent",
        "run_in_background": "false",
        "session_id": sid,
        "tool_input": {"prompt": "go"},
    }, repo_root=str(git_root)))
    assert result == {}


def test_foreground_dispatch_absent_uncalibrated_passes() -> None:
    """run_in_background absent + no calibration sentinel → no_advisory (brick-proof pass)."""
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler
    # No session_id → uncalibrated (not in _BG_CAPABLE_SESSIONS) → conservative pass
    result = _run(_handler({"tool_name": "Agent", "session_id": ""}))
    assert result == {}


def test_foreground_dispatch_absent_calibrated_deny() -> None:
    """run_in_background absent + session previously calibrated (in-memory) → deny.

    Simulates the sequence: first call has run_in_background="true" (calibrates the
    session_id into _BG_CAPABLE_SESSIONS); second call omits the param (calibrated
    absent = deliberate foreground choice) → deny.

    This is the in-memory equivalent of the old file-sentinel calibration path (D7).
    """
    import coordinator_core.hooks.nudge_foreground_agent_dispatch as mod
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler

    # Use a unique session_id to avoid state leakage from other tests
    calibrated_sid = "test-calib-inmemory-aa11bb22"

    # Step 1: present-and-true call — calibrates the session in-memory.
    # session_id must be non-empty and format-valid for calibration to fire.
    result1 = _run(_handler(
        {"tool_name": "Agent", "run_in_background": "true", "session_id": calibrated_sid}
    ))
    assert result1 == {}, "present-and-true should pass"
    assert calibrated_sid in mod._BG_CAPABLE_SESSIONS, (
        "session_id should be in _BG_CAPABLE_SESSIONS after a present-value call"
    )

    # Step 2: absent call on the same session_id — should deny (calibrated absent).
    # We skip git-root resolution by mocking _resolve_git_root to return "" so no
    # .foreground-ok path is checked (keeps test hermetic, no real .git I/O).
    import unittest.mock as mock
    with mock.patch.object(mod, "_resolve_git_root", return_value=""):
        result2 = _run(_handler(
            {"tool_name": "Agent", "session_id": calibrated_sid}
        ))
    _assert_deny(result2, "PreToolUse")

    # Cleanup: remove the test session_id so it doesn't affect subsequent tests
    mod._BG_CAPABLE_SESSIONS.discard(calibrated_sid)


def test_foreground_deny_message_carries_no_unlock_mechanism() -> None:
    """docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-keys.md AC-2/AC-3.

    A subagent-audience deny (no `agent_id`/backpointer resolvable to a real EM) must
    not carry the `.foreground-ok` touch recipe, the sentinel path, the session id, or
    a hand-rolled doc pointer.
    """
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler
    sid = "test-no-unlock-deny-01"
    result = _run(_handler(
        {
            "tool_name": "Agent",
            "run_in_background": "false",
            "session_id": sid,
            "agent_id": "subagent-abc123",
        }
    ))
    _assert_deny(result, "PreToolUse")
    _assert_no_unlock_mechanism(_hso(result)["permissionDecisionReason"], sid)


def test_foreground_reroute_notice_carries_no_unlock_mechanism() -> None:
    """docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-keys.md AC-2/AC-3.

    A subagent-audience reroute notice must not carry the `.foreground-ok` touch
    recipe, the sentinel path, the session id, or a hand-rolled doc pointer either.
    """
    from coordinator_core.hooks.nudge_foreground_agent_dispatch import _handler
    sid = "test-no-unlock-reroute-01"
    result = _run(_handler({
        "tool_name": "Agent",
        "run_in_background": "false",
        "session_id": sid,
        "tool_input": {"prompt": "go"},
        "agent_id": "subagent-abc123",
    }))
    _assert_no_unlock_mechanism(_hso(result)["additionalContext"], sid)


# ---------------------------------------------------------------------------
# C2 — suggest_sonnet_research
# ---------------------------------------------------------------------------

def test_sonnet_research_subagent_suppressed() -> None:
    """agent_id present → no_advisory (subagent suppression — already a delegated researcher)."""
    from coordinator_core.hooks.suggest_sonnet_research import _handler
    assert _run(_handler({"agent_id": "abc123def456"})) == {}


def test_sonnet_research_fires_allow_advisory() -> None:
    """agent_id absent → allow_advisory with DELEGATION REQUIRED message."""
    from coordinator_core.hooks.suggest_sonnet_research import _handler
    result = _run(_handler({}))
    _assert_allow_advisory(result)
    assert "DELEGATION REQUIRED" in _hso(result)["additionalContext"]


def test_sonnet_research_advisory_shape_correct() -> None:
    """allow_advisory shape: permissionDecision=allow, hookEventName=PreToolUse."""
    from coordinator_core.hooks.suggest_sonnet_research import _handler
    result = _run(_handler({"agent_id": ""}))  # "" is absent per _payload contract
    hso = _hso(result)
    assert hso["permissionDecision"] == "allow"
    assert hso["hookEventName"] == "PreToolUse"


# Review: code-reviewer (Finding 5) — _deep_research_plugin_dir()'s content-root
# resolution branches (success, unresolvable, and the raise-degrades path) were
# previously unexercised by an assertion: the _handler tests above call through to
# the real resolver on whatever machine runs the suite rather than proving the
# degrade contract.
#
# These tests patch coordinator_core.resolve_coordinator_clone.resolve_content_root
# — the native in-process peer the hook now delegates to. The prior version patched
# the module's `subprocess.run`, which pinned the retired
# `~/.claude/bin/resolve-coordinator-clone` CLI spawn; that path vanished with the
# settings-home migration, so resolution failed on every real fire and the hook
# silently emitted the "plugin absent" advisory variant.

def test_deep_research_plugin_dir_resolves_on_success() -> None:
    """A resolved content root -> pipelines/deep-research beneath it."""
    from unittest.mock import patch

    import coordinator_core.hooks.suggest_sonnet_research as _mod
    import coordinator_core.resolve_coordinator_clone as _rcc

    with patch.object(_rcc, "resolve_content_root", return_value="/fake/clone-root") as mock_resolve:
        result = _mod._deep_research_plugin_dir()

    assert result == os.path.join("/fake/clone-root", "pipelines", "deep-research")
    mock_resolve.assert_called_once()


def test_deep_research_plugin_dir_empty_root_degrades_to_none() -> None:
    """An empty resolved root (resolver ran but produced nothing) -> None, not a bare join."""
    from unittest.mock import patch

    import coordinator_core.hooks.suggest_sonnet_research as _mod
    import coordinator_core.resolve_coordinator_clone as _rcc

    with patch.object(_rcc, "resolve_content_root", return_value=""):
        result = _mod._deep_research_plugin_dir()

    assert result is None


def test_deep_research_plugin_dir_resolution_error_degrades_to_none() -> None:
    """A raising resolver must degrade to None, not propagate — this call runs inside
    asyncio.to_thread on a PreToolUse-hook hot path and must never brick a tool call."""
    from unittest.mock import patch

    import coordinator_core.hooks.suggest_sonnet_research as _mod
    import coordinator_core.resolve_coordinator_clone as _rcc
    from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError

    with patch.object(
        _rcc, "resolve_content_root",
        side_effect=ResolveCoordinatorCloneError("no git-backed clone found"),
    ):
        result = _mod._deep_research_plugin_dir()

    assert result is None


def test_deep_research_plugin_dir_oserror_degrades_to_none() -> None:
    """OSError from the resolver must degrade to None, matching prior behavior."""
    from unittest.mock import patch

    import coordinator_core.hooks.suggest_sonnet_research as _mod
    import coordinator_core.resolve_coordinator_clone as _rcc

    with patch.object(_rcc, "resolve_content_root", side_effect=OSError("not found")):
        result = _mod._deep_research_plugin_dir()

    assert result is None


# ---------------------------------------------------------------------------
# C5 — nudge_em_code_dispatch
# ---------------------------------------------------------------------------

def test_em_code_dispatch_subagent_suppressed() -> None:
    """agent_id present → no_advisory (executors write code; subagent bypass unconditional)."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler
    assert _run(_handler(
        {"agent_id": "abc123def456", "file_path": "src/new_module.py"}
    )) == {}


def test_em_code_dispatch_doc_extension_passes() -> None:
    """file_path with .md → no_advisory (doc/data extension denylist)."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler
    assert _run(_handler({"file_path": "docs/guide.md"})) == {}


def test_em_code_dispatch_yaml_extension_passes() -> None:
    """file_path with .yaml → no_advisory."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler
    assert _run(_handler({"file_path": "config/settings.yaml"})) == {}


def test_em_code_dispatch_json_extension_passes() -> None:
    """file_path with .json → no_advisory."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler
    assert _run(_handler({"file_path": "package.json"})) == {}


def test_em_code_dispatch_file_path_absent_passes() -> None:
    """file_path absent → no_advisory."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler
    assert _run(_handler({})) == {}


def test_em_code_dispatch_py_file_fires_context_only() -> None:
    """EM writing a .py file → context_only (no permissionDecision; advisory only)."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler
    result = _run(_handler({"file_path": "coordinator_core/new_module.py"}))
    _assert_context_only(result, "PreToolUse")
    assert "em-code-dispatch nudge" in _hso(result)["additionalContext"]


def test_em_code_dispatch_sh_file_fires_context_only() -> None:
    """EM writing a .sh script → context_only nudge."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler
    result = _run(_handler({"file_path": "bin/new-script.sh"}))
    _assert_context_only(result, "PreToolUse")


def test_em_code_dispatch_context_only_has_no_permission_decision() -> None:
    """context_only envelope MUST NOT contain permissionDecision (shape (b) contract)."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler
    result = _run(_handler({"file_path": "src/main.py"}))
    assert "permissionDecision" not in _hso(result)


# ---------------------------------------------------------------------------
# C6 — nudge_unauthorized_handoff
# ---------------------------------------------------------------------------

def test_unauthorized_handoff_non_write_tool_passes() -> None:
    """tool_name != 'Write' → no_advisory (belt-and-braces; hook.json is Write-only)."""
    from coordinator_core.hooks.nudge_unauthorized_handoff import _handler
    assert _run(_handler(
        {"tool_name": "Edit", "file_path": "state/handoffs/foo.md"}
    )) == {}


def test_unauthorized_handoff_non_handoff_path_passes() -> None:
    """Write to non-handoff / non-spinoff path → no_advisory."""
    from coordinator_core.hooks.nudge_unauthorized_handoff import _handler
    assert _run(_handler(
        {"tool_name": "Write", "file_path": "state/orientation_cache.md"}
    )) == {}


def test_unauthorized_handoff_fires_post_advisory() -> None:
    """Write to state/handoffs/ without suppression → post_advisory nudge."""
    from coordinator_core.hooks.nudge_unauthorized_handoff import _handler
    result = _run(_handler(
        {
            "tool_name": "Write",
            "file_path": "state/handoffs/2026-07-04-test.md",
            "content": "---\nstatus: open\n---\n# handoff",
        }
    ))
    _assert_post_advisory(result)
    context = _hso(result)["additionalContext"]
    assert "[nudge]" in context
    assert "state/handoffs" in context


def test_unauthorized_handoff_spinoffs_path_fires() -> None:
    """Write to tasks/spinoffs/ → post_advisory nudge."""
    from coordinator_core.hooks.nudge_unauthorized_handoff import _handler
    result = _run(_handler(
        {
            "tool_name": "Write",
            "file_path": "tasks/spinoffs/2026-07-04-topic.md",
            "content": "---\nkind: spinoff\n---\n# topic",
        }
    ))
    # Not an install-leg spinoff (no install_chain_order) → should nudge
    _assert_post_advisory(result)


def test_unauthorized_handoff_kind_recovery_suppressed() -> None:
    """Content with 'kind: recovery' in leading frontmatter → no_advisory."""
    from coordinator_core.hooks.nudge_unauthorized_handoff import _handler
    content = "---\nkind: recovery\npredecessor: abc1234\n---\n# recovery handoff"
    assert _run(_handler(
        {"tool_name": "Write", "file_path": "state/handoffs/recovery.md", "content": content}
    )) == {}


def test_unauthorized_handoff_install_leg_spinoff_suppressed() -> None:
    """Install-leg spinoff (kind:spinoff + install_chain_order) → no_advisory."""
    from coordinator_core.hooks.nudge_unauthorized_handoff import _handler
    content = (
        "---\n"
        "kind: spinoff\n"
        "install_chain_order: 1\n"
        "---\n"
        "# install leg"
    )
    assert _run(_handler(
        {"tool_name": "Write", "file_path": "tasks/spinoffs/install-leg.md", "content": content}
    )) == {}


def test_unauthorized_handoff_spinoff_without_chain_order_fires() -> None:
    """kind:spinoff alone (no install_chain_order) → NOT suppressed → post_advisory."""
    from coordinator_core.hooks.nudge_unauthorized_handoff import _handler
    content = "---\nkind: spinoff\n---\n# regular spinoff"
    result = _run(_handler(
        {"tool_name": "Write", "file_path": "tasks/spinoffs/regular.md", "content": content}
    ))
    _assert_post_advisory(result)


# ---------------------------------------------------------------------------
# C7 — postuse_advisory_dispatch
# ---------------------------------------------------------------------------

def test_postuse_no_session_id_returns_no_advisory() -> None:
    """session_id absent → no_advisory (short-circuit: nothing to check)."""
    from coordinator_core.hooks.postuse_advisory_dispatch import _handler
    assert _run(_handler({})) == {}


def test_postuse_empty_session_id_returns_no_advisory() -> None:
    """session_id == '' (absent per _payload contract) → no_advisory."""
    from coordinator_core.hooks.postuse_advisory_dispatch import _handler
    assert _run(_handler({"session_id": ""})) == {}


def test_postuse_session_no_matching_sentinels_returns_no_advisory() -> None:
    """session_id present but no /tmp sentinels, no agents_dir → no_advisory.

    Both checks fail-open: context-pressure finds no compaction sentinel and no
    transcript → returns ''; runtime-tripwire finds no .agents dir → returns ''.
    """
    import coordinator_core.hooks.postuse_advisory_dispatch as pad_mod
    from coordinator_core.hooks.postuse_advisory_dispatch import _handler
    # Use a test-scoped session id to avoid collisions with real sessions
    test_sid = "test-c8-roundtrip-nosentinel-9f3a"
    state_path = pad_mod._advisory_state_path(tempfile.gettempdir(), test_sid)
    # Clean up any pre-existing durable throttle state from a prior run.
    _unlink_if_exists(state_path)
    result = _run(_handler({"session_id": test_sid, "transcript_path": ""}))
    assert result == {}
    # Cleanup durable throttle state file written as a side effect of Phase 2.
    _unlink_if_exists(state_path)


def test_postuse_merge_contract_both_fire() -> None:
    """Both checks fire → post_advisory with texts merged via blank-line separator.

    Tests the merge contract by injecting a compaction-occurred sentinel and a
    matching runtime-tripwire structure under a temp .git tree.
    """
    import time
    import unittest.mock as mock
    from pathlib import Path
    import coordinator_core.hooks.postuse_advisory_dispatch as pad_mod
    from coordinator_core.hooks.postuse_advisory_dispatch import _handler

    # Build a minimal fake git tree for the runtime-tripwire check
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # --- Runtime-tripwire setup ---
        test_sid = "test-c8-merge-12ab34cd"
        agent_dir = tmp / ".git" / "coordinator-sessions" / ".agents" / test_sid
        agent_dir.mkdir(parents=True)

        em_sid = "test-c8-em-session-56ef"
        (agent_dir / "em-session-id.txt").write_text(em_sid + "\n")

        em_session_dir = tmp / ".git" / "coordinator-sessions" / em_sid
        em_session_dir.mkdir(parents=True)

        # dispatched-agents.txt: agentId\tmodel\tsubagent_type\tdispatched-at
        # Set dispatched_at far in the past to exceed the threshold
        past_ts = int(time.time()) - 3600  # 60 min ago — well past any threshold
        dispatch_file = em_session_dir / "dispatched-agents.txt"
        dispatch_file.write_text(f"{test_sid}\tclaude-sonnet-4-5\texecutor\t{past_ts}\n")

        # --- Context-pressure setup ---
        # Write a compaction sentinel pointing to a transcript path that is smaller than 85% of pre_size
        transcript = tmp / "transcript.jsonl"
        # Write a small transcript with a model field so Phase 2 detects the model
        transcript.write_text('{"model": "claude-sonnet-4-5", "role": "assistant", "content": "hi"}\n')

        # Review: code-reviewer (B-F3) — use tempfile.gettempdir() matching the handler's path.
        compaction_sentinel = os.path.join(
            tempfile.gettempdir(), f"compaction-occurred-{test_sid}"
        )
        pre_size = os.path.getsize(str(transcript)) * 10  # pre_size >> post_size → real compaction
        Path(compaction_sentinel).write_text(str(pre_size))

        # Clean up any pre-existing durable state for this test-scoped session id
        # (fresh id per run, but defensive against a prior interrupted run).
        cp_state_path = pad_mod._advisory_state_path(tempfile.gettempdir(), test_sid)
        rt_bark_sentinel = os.path.join(tempfile.gettempdir(), f"rt-bark-once-{test_sid}")
        _unlink_if_exists(cp_state_path)
        _unlink_if_exists(rt_bark_sentinel)

        # The runtime-tripwire check uses the real git root (which won't have our .agents
        # dir), so we exercise the MERGE contract by patching _check_runtime_tripwire_sync
        # to return a known string.
        mock_rt = "RUNTIME TRIPWIRE — test-injected tripwire text"

        def fake_rt(session_id, agent_id):
            return mock_rt

        with mock.patch.object(pad_mod, "_check_runtime_tripwire_sync", side_effect=fake_rt):
            result = _run(_handler(
                {
                    "session_id": test_sid,
                    "transcript_path": str(transcript),
                    "agent_id": "",
                }
            ))

        # The handler itself now consumes (deletes) the compaction sentinel —
        # this is a defensive no-op safety net, not evidence the handler didn't.
        _unlink_if_exists(compaction_sentinel)
        # Clean up durable throttle/dedup state a Phase 2 call could have written
        # (Phase 1 returns early here, but guard against that assumption drifting).
        _unlink_if_exists(cp_state_path)
        _unlink_if_exists(rt_bark_sentinel)

    # When both fire, the result is post_advisory with merged text
    hso = _hso(result)
    assert hso["hookEventName"] == "PostToolUse"
    assert "additionalContext" in hso
    context = hso["additionalContext"]
    # Review: code-reviewer (B-F4) — strengthen from 'or' to separate asserts that verify
    # BOTH sub-checks fired AND the blank-line separator merge contract is honoured.
    assert "COMPACTION" in context
    assert mock_rt in context
    assert "\n\n" in context  # blank-line separator as per handler merge-contract docstring


def test_postuse_result_shape_is_post_advisory_when_fires() -> None:
    """When postuse fires, the envelope is PostToolUse (not PreToolUse) — shape (d)."""
    import unittest.mock as mock
    import coordinator_core.hooks.postuse_advisory_dispatch as pad_mod
    from coordinator_core.hooks.postuse_advisory_dispatch import _handler

    test_sid = "test-c8-shape-verif-cc99"

    with mock.patch.object(pad_mod, "_check_context_pressure_sync", return_value="cp advisory text"):
        with mock.patch.object(pad_mod, "_check_runtime_tripwire_sync", return_value=""):
            result = _run(_handler({"session_id": test_sid}))

    _assert_post_advisory(result)
    assert "cp advisory text" in _hso(result)["additionalContext"]


# ---------------------------------------------------------------------------
# C5 — nudge_em_code_dispatch sentinel suppression (B-F5)
# ---------------------------------------------------------------------------

def test_em_code_dispatch_nudge_ok_sentinel_suppresses(tmp_path) -> None:
    """Bypass 3: coordinator-dispatch-nudge-ok-{sid} sentinel present → no_advisory.

    Review: code-reviewer (B-F5) — covers the production suppression path for
    authorized inline runs; a regression in sentinel-path construction would cause
    the nudge to fire on every authorized inline run.
    """
    import tempfile
    from pathlib import Path
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler

    sid = "test-bypass3-sentinel-aa99"
    p = Path(tempfile.gettempdir()) / f"coordinator-dispatch-nudge-ok-{sid}"
    p.touch()
    try:
        result = _run(_handler({"file_path": "src/main.py", "session_id": sid}))
        assert result == {}
    finally:
        p.unlink(missing_ok=True)


def test_em_code_dispatch_autonomous_sentinel_suppresses(tmp_path) -> None:
    """Bypass 4: autonomous-run-{sid} sentinel present → no_advisory.

    Review: code-reviewer (B-F5) — covers the autonomous-run suppression path;
    a regression here would cause the nudge to fire on every autonomous-mode run.
    """
    import tempfile
    from pathlib import Path
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler

    sid = "test-bypass4-sentinel-bb88"
    p = Path(tempfile.gettempdir()) / f"autonomous-run-{sid}"
    p.touch()
    try:
        result = _run(_handler({"file_path": "src/main.py", "session_id": sid}))
        assert result == {}
    finally:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# C6 — nudge_unauthorized_handoff transcript suppression (B-F6)
# ---------------------------------------------------------------------------

def test_unauthorized_handoff_command_tag_in_transcript_suppresses(tmp_path) -> None:
    """Authoring-skill <command-name> tag in transcript → no_advisory (suppressed).

    Review: code-reviewer (B-F6) — covers _authoring_skill_active_sync check 1
    (command-name tag match). Untested regression would cause nudge to fire on
    every legitimate /handoff invocation.
    """
    from coordinator_core.hooks.nudge_unauthorized_handoff import _handler

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("<command-name>handoff</command-name>\n")
    result = _run(_handler({
        "tool_name": "Write",
        "file_path": "state/handoffs/test.md",
        "content": "---\nstatus: open\n---",
        "transcript_path": str(transcript),
    }))
    assert result == {}


def test_unauthorized_handoff_coordinator_skill_in_tail_suppresses(tmp_path) -> None:
    """coordinator:handoff skill invocation in transcript tail → no_advisory (suppressed).

    Review: code-reviewer (B-F6) — covers _authoring_skill_active_sync check 2
    (generous-tail regex). Tests the SIGPIPE-avoidance in-memory-read path.
    """
    from coordinator_core.hooks.nudge_unauthorized_handoff import _handler

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"input":{"skill":"coordinator:handoff"}}\n')
    result = _run(_handler({
        "tool_name": "Write",
        "file_path": "state/handoffs/test.md",
        "content": "---\nstatus: open\n---",
        "transcript_path": str(transcript),
    }))
    assert result == {}


# ---------------------------------------------------------------------------
# dual-home-sentinel-trap C4 — single-home contract for the two dispatch-nudge
# suppression sentinels (.dispatch-nudge-ok, .autonomous).
#
# Spec backlink: docs/plans/2026-07-31-dual-home-sentinel-trap.md § C4.
#
# Negative-spec: the negative half of each pair below is the load-bearing
# assertion — it must go RED the moment anyone re-adds an OR-branch checking
# a git-tree candidate location for either sentinel. C1/C2 deleted the dead
# git-tree lanes (`<repo_root>/coordinator-sessions/<sid>/.dispatch-nudge-ok`
# and `.../.autonomous`) that nothing ever wrote; this is the regression net.
# ---------------------------------------------------------------------------

def test_dispatch_nudge_ok_tmpdir_only_suppresses() -> None:
    """.dispatch-nudge-ok: writing ONLY the tmpdir home suppresses the nudge."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler
    from coordinator_core.session.dispatch_nudge_sentinel import sentinel_path

    sid = "test-c4-dispatch-nudge-tmpdir-only"
    p = sentinel_path(sid)
    p.touch()
    try:
        result = _run(_handler({"file_path": "src/main.py", "session_id": sid}))
        assert result == {}
    finally:
        p.unlink(missing_ok=True)


def test_dispatch_nudge_ok_git_tree_only_does_not_suppress(tmp_path) -> None:
    """.dispatch-nudge-ok: writing ONLY the (now-dead) git-tree home does NOT
    suppress — the load-bearing negative half. Regresses the instant an
    OR-branch re-checks a git-tree candidate for this sentinel."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler

    git_root = tmp_path / ".git"
    sid = "test-c4-dispatch-nudge-git-only"
    git_home = git_root / "coordinator-sessions" / sid / ".dispatch-nudge-ok"
    git_home.parent.mkdir(parents=True)
    git_home.touch()

    result = _run(_handler(
        {"file_path": "src/main.py", "session_id": sid},
        repo_root=str(git_root),
    ))
    _assert_context_only(result, "PreToolUse")


def test_autonomous_tmpdir_only_suppresses() -> None:
    """.autonomous: writing ONLY the tmpdir home suppresses the nudge."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler
    from coordinator_core.session.autonomous_sentinel import sentinel_path

    sid = "test-c4-autonomous-tmpdir-only"
    p = sentinel_path(sid)
    p.touch()
    try:
        result = _run(_handler({"file_path": "src/main.py", "session_id": sid}))
        assert result == {}
    finally:
        p.unlink(missing_ok=True)


def test_autonomous_git_tree_only_does_not_suppress(tmp_path) -> None:
    """.autonomous: writing ONLY the (now-dead) git-tree home does NOT
    suppress — the load-bearing negative half. Regresses the instant an
    OR-branch re-checks a git-tree candidate for this sentinel."""
    from coordinator_core.hooks.nudge_em_code_dispatch import _handler

    git_root = tmp_path / ".git"
    sid = "test-c4-autonomous-git-only"
    git_home = git_root / "coordinator-sessions" / sid / ".autonomous"
    git_home.parent.mkdir(parents=True)
    git_home.touch()

    result = _run(_handler(
        {"file_path": "src/main.py", "session_id": sid},
        repo_root=str(git_root),
    ))
    _assert_context_only(result, "PreToolUse")


# ---------------------------------------------------------------------------
# AC10 — op scope-class unchanged by this plan. Assert on mapping VALUES, not
# on line content — a line-keyed snapshot would pass while checking nothing.
# This is the only home for AC10 in the whole slate.
# ---------------------------------------------------------------------------

def test_ac10_nudge_foreground_agent_dispatch_scope_class_unchanged() -> None:
    """op-key scope and authz classification for hooks.nudge_foreground_agent_dispatch
    are untouched by the dual-home-sentinel-trap plan (that op is C5b, out of scope
    here) — pinned so a future edit to either mapping is a deliberate, reviewed change."""
    from coordinator_core.op_scopes import OP_KEY_SCOPE
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass

    assert OP_KEY_SCOPE["hooks.nudge_foreground_agent_dispatch"] == "common_dir"
    assert (
        OP_CLASSIFICATION["hooks.nudge_foreground_agent_dispatch"] == OpClass.MUTATING
    )


# ---------------------------------------------------------------------------
# Direct unit coverage of C1 — coordinator_core.session.dispatch_nudge_sentinel.
# ---------------------------------------------------------------------------

def test_dispatch_nudge_sentinel_negative_spec_names_both_prohibitions() -> None:
    """Module docstring carries a Negative-spec block naming both prohibitions:
    hardcoded /tmp, and a second (git-tree / OR-list) candidate location."""
    import coordinator_core.session.dispatch_nudge_sentinel as mod

    doc = mod.__doc__ or ""
    assert "Negative-spec:" in doc
    assert "/tmp" in doc
    assert "tempfile.gettempdir()" in doc
    assert "second candidate location" in doc or "OR-list" in doc


def test_dispatch_nudge_sentinel_path_rooted_at_platform_tempdir() -> None:
    """sentinel_path(sid) resolves under tempfile.gettempdir() and ends in the
    expected filename — the single-source resolver's own contract."""
    import tempfile
    from pathlib import Path
    from coordinator_core.session.dispatch_nudge_sentinel import sentinel_path

    sid = "test-c4-sentinel-path-known-sid"
    result = sentinel_path(sid)
    assert result.parent == Path(tempfile.gettempdir())
    assert result.name == f"coordinator-dispatch-nudge-ok-{sid}"
