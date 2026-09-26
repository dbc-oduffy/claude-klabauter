
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.hooks import allow_emitted_workflow_fire as aewf
from coordinator_core.hooks import block_dispatch_suite_invocation as bdsi
from coordinator_core.hooks import block_workflow_foreign_emission as bwfe
from coordinator_core.hooks import block_workflow_unmodeled_agent as bwua
from coordinator_core.hooks import block_worktree_tool as bwt
from coordinator_core.hooks import nudge_multiwave_workflow as nmw
from coordinator_core.hooks import nudge_workflow_authoring_trampoline as nwat
from coordinator_core.hooks import strip_worktree_isolation as swi


@pytest.mark.parametrize(
    "op_name",
    [
        "hooks.block_workflow_foreign_emission",
        "hooks.block_workflow_unmodeled_agent",
        "hooks.allow_emitted_workflow_fire",
        "hooks.nudge_workflow_authoring_trampoline",
        "hooks.nudge_multiwave_workflow",
        "hooks.block_dispatch_suite_invocation",
        "hooks.strip_worktree_isolation",
        "hooks.block_worktree_tool",
    ],
)
def test_op_is_registered(op_name):
    assert op_name in ipc._REGISTRY


def test_bwfe_no_advisory_on_non_workflow(tmp_path):
    result = bwfe._handler({"tool_name": "Bash"})
    assert result == {}


def test_bwfe_sha_mismatch_denies_and_names_settings_home_launcher(tmp_path):
    script = tmp_path / "plan.workflow.mjs"
    script.write_text("console.log('a');\n", encoding="utf-8")
    receipt = script.with_name(script.name + ".emitted.json")
    receipt.write_text(
        json.dumps({"sha256": "0" * 64, "session_id": "abc12345"}),
        encoding="utf-8",
    )
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
            "session_id": "abc12345",
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    assert "changed after emission" in reason
    assert "--restamp" in reason
    assert "python3" not in reason


def test_bwfe_session_mismatch_denies(tmp_path):
    script = tmp_path / "plan.workflow.mjs"
    script.write_bytes(b"console.log('a');\n")
    actual_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    receipt = script.with_name(script.name + ".emitted.json")
    receipt.write_text(
        json.dumps({"sha256": actual_sha, "session_id": "peer-session"}),
        encoding="utf-8",
    )
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
            "session_id": "this-session",
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "DIFFERENT session" in hso["permissionDecisionReason"]


def test_bwfe_verifying_receipt_no_advisory(tmp_path):
    script = tmp_path / "plan.workflow.mjs"
    script.write_bytes(b"console.log('a');\n")
    actual_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    receipt = script.with_name(script.name + ".emitted.json")
    receipt.write_text(
        json.dumps({"sha256": actual_sha, "session_id": "same"}), encoding="utf-8"
    )
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
            "session_id": "same",
        }
    )
    assert result == {}


def test_bwfe_no_receipt_no_advisory(tmp_path):
    script = tmp_path / "hand-authored.workflow.mjs"
    script.write_text("console.log('a');\n", encoding="utf-8")
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
        }
    )
    assert result == {}


def test_bwfe_sha_mismatch_denies_through_the_wrapped_envelope(tmp_path):
    script = tmp_path / "plan.workflow.mjs"
    script.write_text("console.log('a');\n", encoding="utf-8")
    receipt = script.with_name(script.name + ".emitted.json")
    receipt.write_text(
        json.dumps({"sha256": "0" * 64, "session_id": "abc12345"}),
        encoding="utf-8",
    )
    result = bwfe._handler(
        {
            "payload": {
                "tool_name": "Workflow",
                "tool_input": {"scriptPath": str(script)},
                "cwd": str(tmp_path),
                "session_id": "abc12345",
            }
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_aewf_never_denies(tmp_path):
    for payload in (
        {},
        {"tool_name": "Workflow", "tool_input": {}},
        {"tool_name": "Workflow", "tool_input": {"script": "console.log(1)"}},
        {"tool_name": "Workflow", "tool_input": {"scriptPath": "/nope/nope.mjs"}},
    ):
        result = aewf._handler(payload)
        if result:
            decision = result.get("hookSpecificOutput", {}).get("permissionDecision")
            assert decision != "deny"


def test_aewf_allows_on_verifying_receipt(tmp_path):
    script = tmp_path / "plan.workflow.mjs"
    script.write_bytes(b"console.log('a');\n")
    actual_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    receipt = script.with_name(script.name + ".emitted.json")
    receipt.write_text(
        json.dumps({"sha256": actual_sha, "session_id": "s1", "plan": "my-plan.md"}),
        encoding="utf-8",
    )
    result = aewf._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
            "session_id": "s1",
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "my-plan.md" in hso["additionalContext"]


def test_aewf_silent_for_inline_script():
    result = aewf._handler(
        {"tool_name": "Workflow", "tool_input": {"script": "console.log(1)"}}
    )
    assert result == {}


def test_swi_strips_worktree_isolation():
    result = swi._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": "x.mjs", "isolation": "worktree"},
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["updatedInput"] == {"scriptPath": "x.mjs"}
    assert "isolation" not in hso["updatedInput"]
    assert "worktree" in hso["additionalContext"].lower()


def test_swi_passes_through_other_isolation_values():
    result = swi._handler(
        {"tool_name": "Workflow", "tool_input": {"isolation": "remote"}}
    )
    assert result == {}


def test_swi_no_advisory_on_non_workflow():
    result = swi._handler(
        {"tool_name": "Agent", "tool_input": {"isolation": "worktree"}}
    )
    assert result == {}


def test_bwt_denies_enter_worktree():
    result = bwt._handler({"tool_name": "EnterWorktree"})
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "Worktrees banned" in hso["permissionDecisionReason"]


def test_bwt_allows_exit_worktree():
    result = bwt._handler({"tool_name": "ExitWorktree"})
    assert result == {}


def test_bwt_no_advisory_on_other_tool():
    result = bwt._handler({"tool_name": "Bash"})
    assert result == {}


def test_bwt_denies_enter_worktree_through_the_wrapped_envelope():
    result = bwt._handler({"payload": {"tool_name": "EnterWorktree"}})
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_nwat_no_advisory_without_real_session(tmp_path):
    result = nwat._handler(
        {
            "tool_name": "Skill",
            "tool_input": {"skill": "workflow-authoring"},
            "session_id": "not-a-uuid",
        }
    )
    assert result == {}


def test_nwat_no_advisory_for_scriptpath_launch():
    result = nwat._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": "x.mjs"},
            "session_id": "11111111-1111-4111-8111-111111111111",
        }
    )
    assert result == {}


def test_nwat_no_advisory_for_other_skill():
    result = nwat._handler(
        {
            "tool_name": "Skill",
            "tool_input": {"skill": "unrelated-skill"},
            "session_id": "11111111-1111-4111-8111-111111111111",
        }
    )
    assert result == {}


def test_nmw_no_advisory_on_subagent_dispatch():
    result = nmw._handler(
        {
            "tool_name": "Agent",
            "agent_id": "some-agent",
            "tool_input": {"subagent_type": "coordinator:executor"},
            "session_id": "11111111-1111-4111-8111-111111111111",
        }
    )
    assert result == {}


def test_nmw_no_advisory_for_non_write_capable_type():
    result = nmw._handler(
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "Explore"},
            "session_id": "11111111-1111-4111-8111-111111111111",
        }
    )
    assert result == {}


def test_nmw_env_override_suppresses():
    result = nmw._handler(
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "coordinator:executor"},
            "session_id": "11111111-1111-4111-8111-111111111111",
            "env": {"COORDINATOR_OVERRIDE_MULTIWAVE_WORKFLOW": "1"},
        }
    )
    assert result == {}


def test_bdsi_no_advisory_on_non_dispatch_tool():
    result = bdsi._handler({"tool_name": "Bash"})
    assert result == {}


_BDSI_FIRING_PROMPT = "Run the full test suite: python3 -m pytest"


def test_bdsi_denies_imperative_suite_command():
    result = bdsi._handler(
        {"tool_name": "Agent", "tool_input": {"prompt": _BDSI_FIRING_PROMPT}}
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "Tier-" in hso["permissionDecisionReason"]


def test_bdsi_override_marker_suppresses():
    result = bdsi._handler(
        {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": (
                    "COORDINATOR-OVERRIDE-DISPATCH-SUITE-GUARD: verifying breadth\n"
                    + _BDSI_FIRING_PROMPT
                )
            },
        }
    )
    assert result == {}


def test_bdsi_env_override_suppresses():
    result = bdsi._handler(
        {
            "tool_name": "Agent",
            "tool_input": {"prompt": _BDSI_FIRING_PROMPT},
            "env": {"COORDINATOR_OVERRIDE_DISPATCH_SUITE_GUARD": "1"},
        }
    )
    assert result == {}


def test_bdsi_denies_imperative_suite_command_through_the_wrapped_envelope():
    result = bdsi._handler(
        {"payload": {"tool_name": "Agent", "tool_input": {"prompt": _BDSI_FIRING_PROMPT}}}
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_bwua_no_advisory_without_transcript():
    result = bwua._handler(
        {"tool_name": "Workflow", "tool_input": {"script": "agent('x')"}}
    )
    assert result == {}


def test_bwua_no_advisory_on_non_opus_transcript(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-sonnet-4"}\n', encoding="utf-8")
    result = bwua._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": "agent('x')"},
            "transcript_path": str(transcript),
        }
    )
    assert result == {}


def test_bwua_denies_unmodeled_agent_under_opus(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-opus-4"}\n', encoding="utf-8")
    result = bwua._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": "agent('do the thing')"},
            "transcript_path": str(transcript),
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "agent() call" in hso["permissionDecisionReason"]


def test_bwua_allows_fully_modeled_script_under_opus(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-opus-4"}\n', encoding="utf-8")
    result = bwua._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": "agent('x', {model: 'sonnet'})"},
            "transcript_path": str(transcript),
        }
    )
    assert result == {}


def test_bwua_partial_modeled_warns_via_additional_context(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-opus-4"}\n', encoding="utf-8")
    script = "agent('a', {model: 'sonnet'}); agent('b');"
    result = bwua._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": script},
            "transcript_path": str(transcript),
        }
    )
    hso = result["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    assert "2 agent() calls" in hso["additionalContext"]


def test_bwua_env_override_suppresses(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-opus-4"}\n', encoding="utf-8")
    result = bwua._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": "agent('x')"},
            "transcript_path": str(transcript),
            "env": {"COORDINATOR_OVERRIDE_WORKFLOW_MODEL_GUARD": "1"},
        }
    )
    assert result == {}


def test_bwua_count_agent_modeled_matches_ground_truth():
    script = (
        "// this script's twin, don't crash\n"
        "agent('first', {model: 'sonnet'});\n"
        "agent('second');\n"
    )
    stripped = bwua._strip_comments(script)
    agent_n, modeled_n, _ = bwua._count_agent_modeled_with_types(stripped)
    assert agent_n == 2
    assert modeled_n == 1
