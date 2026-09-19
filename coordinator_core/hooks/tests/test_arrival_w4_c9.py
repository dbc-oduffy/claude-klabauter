"""coordinator_core/hooks/tests/test_arrival_w4_c9.py — the W4-C9 arrival
gate for the Workflow/worktree guard family.

Subject: the eight `hooks.<name>` ops this row's own body writes —
`block_workflow_foreign_emission`, `block_workflow_unmodeled_agent`,
`allow_emitted_workflow_fire`, `nudge_workflow_authoring_trampoline`,
`nudge_multiwave_workflow`, `block_dispatch_suite_invocation`,
`strip_worktree_isolation`, `block_worktree_tool` — ported from
DoE-claude's `coordinator/hooks/scripts/*.py` siblings per
docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C9.

Not exhaustive re-coverage of every DoE test assertion (several source
modules run to 500-1000 lines of string/comment-aware JS scanning) — this
file exercises each op's `register_op` registration, its core allow/deny/
advisory decision branches, and the path-resolution adaptations this port
made (settings-home launcher naming, zero-spawn git-root resolution,
plugin-content-root doctrine-asset probing), against real inputs, not
stubs.
"""

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


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# block_workflow_foreign_emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bwfe_no_advisory_on_non_workflow(tmp_path):
    result = await bwfe._handler({"tool_name": "Bash"})
    assert result == {}


@pytest.mark.asyncio
async def test_bwfe_sha_mismatch_denies_and_names_settings_home_launcher(tmp_path):
    script = tmp_path / "plan.workflow.mjs"
    script.write_text("console.log('a');\n", encoding="utf-8")
    receipt = script.with_name(script.name + ".emitted.json")
    receipt.write_text(
        json.dumps({"sha256": "0" * 64, "session_id": "abc12345"}),
        encoding="utf-8",
    )
    result = await bwfe._handler(
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
    assert "changed after it was emitted" in reason
    assert "--restamp" in reason
    # Never the old plugin-root python3 invocation shape.
    assert "python3" not in reason


@pytest.mark.asyncio
async def test_bwfe_session_mismatch_denies(tmp_path):
    script = tmp_path / "plan.workflow.mjs"
    script.write_bytes(b"console.log('a');\n")
    actual_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    receipt = script.with_name(script.name + ".emitted.json")
    receipt.write_text(
        json.dumps({"sha256": actual_sha, "session_id": "peer-session"}),
        encoding="utf-8",
    )
    result = await bwfe._handler(
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


@pytest.mark.asyncio
async def test_bwfe_verifying_receipt_no_advisory(tmp_path):
    script = tmp_path / "plan.workflow.mjs"
    script.write_bytes(b"console.log('a');\n")
    actual_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    receipt = script.with_name(script.name + ".emitted.json")
    receipt.write_text(
        json.dumps({"sha256": actual_sha, "session_id": "same"}), encoding="utf-8"
    )
    result = await bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
            "session_id": "same",
        }
    )
    assert result == {}


@pytest.mark.asyncio
async def test_bwfe_no_receipt_no_advisory(tmp_path):
    script = tmp_path / "hand-authored.workflow.mjs"
    script.write_text("console.log('a');\n", encoding="utf-8")
    result = await bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
        }
    )
    assert result == {}


# ---------------------------------------------------------------------------
# allow_emitted_workflow_fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aewf_never_denies(tmp_path):
    """Pinned per source module docstring's own NEVER DENIES contract."""
    for payload in (
        {},
        {"tool_name": "Workflow", "tool_input": {}},
        {"tool_name": "Workflow", "tool_input": {"script": "console.log(1)"}},
        {"tool_name": "Workflow", "tool_input": {"scriptPath": "/nope/nope.mjs"}},
    ):
        result = await aewf._handler(payload)
        if result:
            decision = result.get("hookSpecificOutput", {}).get("permissionDecision")
            assert decision != "deny"


@pytest.mark.asyncio
async def test_aewf_allows_on_verifying_receipt(tmp_path):
    script = tmp_path / "plan.workflow.mjs"
    script.write_bytes(b"console.log('a');\n")
    actual_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    receipt = script.with_name(script.name + ".emitted.json")
    receipt.write_text(
        json.dumps({"sha256": actual_sha, "session_id": "s1", "plan": "my-plan.md"}),
        encoding="utf-8",
    )
    result = await aewf._handler(
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


@pytest.mark.asyncio
async def test_aewf_silent_for_inline_script():
    result = await aewf._handler(
        {"tool_name": "Workflow", "tool_input": {"script": "console.log(1)"}}
    )
    assert result == {}


# ---------------------------------------------------------------------------
# strip_worktree_isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swi_strips_worktree_isolation():
    result = await swi._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": "x.mjs", "isolation": "worktree"},
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["updatedInput"] == {"scriptPath": "x.mjs"}
    assert "isolation" not in hso["updatedInput"]
    assert "worktree" in hso["additionalContext"].lower()


@pytest.mark.asyncio
async def test_swi_passes_through_other_isolation_values():
    result = await swi._handler(
        {"tool_name": "Workflow", "tool_input": {"isolation": "remote"}}
    )
    assert result == {}


@pytest.mark.asyncio
async def test_swi_no_advisory_on_non_workflow():
    result = await swi._handler(
        {"tool_name": "Agent", "tool_input": {"isolation": "worktree"}}
    )
    assert result == {}


# ---------------------------------------------------------------------------
# block_worktree_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bwt_denies_enter_worktree():
    result = await bwt._handler({"tool_name": "EnterWorktree"})
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "Worktrees banned" in hso["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_bwt_allows_exit_worktree():
    result = await bwt._handler({"tool_name": "ExitWorktree"})
    assert result == {}


@pytest.mark.asyncio
async def test_bwt_no_advisory_on_other_tool():
    result = await bwt._handler({"tool_name": "Bash"})
    assert result == {}


# ---------------------------------------------------------------------------
# nudge_workflow_authoring_trampoline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nwat_no_advisory_without_real_session(tmp_path):
    result = await nwat._handler(
        {
            "tool_name": "Skill",
            "tool_input": {"skill": "workflow-authoring"},
            "session_id": "not-a-uuid",
        }
    )
    assert result == {}


@pytest.mark.asyncio
async def test_nwat_no_advisory_for_scriptpath_launch():
    result = await nwat._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": "x.mjs"},
            "session_id": "11111111-1111-4111-8111-111111111111",
        }
    )
    assert result == {}


@pytest.mark.asyncio
async def test_nwat_no_advisory_for_other_skill():
    result = await nwat._handler(
        {
            "tool_name": "Skill",
            "tool_input": {"skill": "unrelated-skill"},
            "session_id": "11111111-1111-4111-8111-111111111111",
        }
    )
    assert result == {}


# ---------------------------------------------------------------------------
# nudge_multiwave_workflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nmw_no_advisory_on_subagent_dispatch():
    result = await nmw._handler(
        {
            "tool_name": "Agent",
            "agent_id": "some-agent",
            "tool_input": {"subagent_type": "coordinator:executor"},
            "session_id": "11111111-1111-4111-8111-111111111111",
        }
    )
    assert result == {}


@pytest.mark.asyncio
async def test_nmw_no_advisory_for_non_write_capable_type():
    result = await nmw._handler(
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "Explore"},
            "session_id": "11111111-1111-4111-8111-111111111111",
        }
    )
    assert result == {}


@pytest.mark.asyncio
async def test_nmw_env_override_suppresses():
    result = await nmw._handler(
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "coordinator:executor"},
            "session_id": "11111111-1111-4111-8111-111111111111",
            "env": {"COORDINATOR_OVERRIDE_MULTIWAVE_WORKFLOW": "1"},
        }
    )
    assert result == {}


# ---------------------------------------------------------------------------
# block_dispatch_suite_invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bdsi_no_advisory_on_non_dispatch_tool():
    result = await bdsi._handler({"tool_name": "Bash"})
    assert result == {}


#: A dispatch prompt the suite classifier DOES deny -- the override tests
#: below must use it too, or they pass whether the override works or not.
_BDSI_FIRING_PROMPT = "Run the full test suite: python3 -m pytest"


@pytest.mark.asyncio
async def test_bdsi_denies_imperative_suite_command():
    result = await bdsi._handler(
        {"tool_name": "Agent", "tool_input": {"prompt": _BDSI_FIRING_PROMPT}}
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "Tier-" in hso["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_bdsi_override_marker_suppresses():
    result = await bdsi._handler(
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


@pytest.mark.asyncio
async def test_bdsi_env_override_suppresses():
    result = await bdsi._handler(
        {
            "tool_name": "Agent",
            "tool_input": {"prompt": _BDSI_FIRING_PROMPT},
            "env": {"COORDINATOR_OVERRIDE_DISPATCH_SUITE_GUARD": "1"},
        }
    )
    assert result == {}


# ---------------------------------------------------------------------------
# block_workflow_unmodeled_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bwua_no_advisory_without_transcript():
    result = await bwua._handler(
        {"tool_name": "Workflow", "tool_input": {"script": "agent('x')"}}
    )
    assert result == {}


@pytest.mark.asyncio
async def test_bwua_no_advisory_on_non_opus_transcript(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-sonnet-4"}\n', encoding="utf-8")
    result = await bwua._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": "agent('x')"},
            "transcript_path": str(transcript),
        }
    )
    assert result == {}


@pytest.mark.asyncio
async def test_bwua_denies_unmodeled_agent_under_opus(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-opus-4"}\n', encoding="utf-8")
    result = await bwua._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": "agent('do the thing')"},
            "transcript_path": str(transcript),
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "agent() call" in hso["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_bwua_allows_fully_modeled_script_under_opus(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-opus-4"}\n', encoding="utf-8")
    result = await bwua._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": "agent('x', {model: 'sonnet'})"},
            "transcript_path": str(transcript),
        }
    )
    assert result == {}


@pytest.mark.asyncio
async def test_bwua_partial_modeled_warns_via_additional_context(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-opus-4"}\n', encoding="utf-8")
    script = "agent('a', {model: 'sonnet'}); agent('b');"
    result = await bwua._handler(
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


@pytest.mark.asyncio
async def test_bwua_env_override_suppresses(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"model":"claude-opus-4"}\n', encoding="utf-8")
    result = await bwua._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": "agent('x')"},
            "transcript_path": str(transcript),
            "env": {"COORDINATOR_OVERRIDE_WORKFLOW_MODEL_GUARD": "1"},
        }
    )
    assert result == {}


def test_bwua_count_agent_modeled_matches_ground_truth():
    """Direct unit exercise of the ported string/comment-aware scanner
    against the two-real-call-site ground truth the source module's own
    2026-07-23 fix documents (a false-positive-triggering apostrophe-in-
    comment case)."""
    script = (
        "// this script's twin, don't crash\n"
        "agent('first', {model: 'sonnet'});\n"
        "agent('second');\n"
    )
    stripped = bwua._strip_comments(script)
    agent_n, modeled_n, _ = bwua._count_agent_modeled_with_types(stripped)
    assert agent_n == 2
    assert modeled_n == 1
