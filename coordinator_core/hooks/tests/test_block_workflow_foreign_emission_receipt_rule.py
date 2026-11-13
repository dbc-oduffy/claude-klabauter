
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.hooks import block_workflow_foreign_emission as bwfe


def _agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_agent_def(agents_dir: Path, name: str, tools: str) -> None:
    (agents_dir / f"{name}.md").write_text(
        f"---\nname: {name}\ntools: {tools}\n---\n\nbody\n", encoding="utf-8"
    )


def _patch_agents_dir(monkeypatch, agents_dir: Path) -> None:
    import coordinator_core.hooks.block_workflow_unmodeled_agent as bwua

    monkeypatch.setattr(bwua, "_agents_dir", lambda: agents_dir)


def test_write_capable_agent_no_receipt_is_denied(tmp_path, monkeypatch):
    agents_dir = _agents_dir(tmp_path)
    _write_agent_def(agents_dir, "executor", "[Read, Edit, Write, Bash]")
    _patch_agents_dir(monkeypatch, agents_dir)

    script = tmp_path / "hand.workflow.mjs"
    script.write_text(
        'agent({ prompt: "do it", agentType: "executor" });\n', encoding="utf-8"
    )
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "no receipt" in hso["permissionDecisionReason"]


def test_untyped_agent_call_no_receipt_is_denied(tmp_path):
    script = tmp_path / "hand.workflow.mjs"
    script.write_text('agent({ prompt: "do it" });\n', encoding="utf-8")
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_every_call_read_only_by_frontmatter_is_exempt(tmp_path, monkeypatch):
    agents_dir = _agents_dir(tmp_path)
    _write_agent_def(agents_dir, "explore", "[Read, Grep, Glob]")
    _patch_agents_dir(monkeypatch, agents_dir)

    script = tmp_path / "hand.workflow.mjs"
    script.write_text(
        'agent({ prompt: "look", agentType: "explore" });\n', encoding="utf-8"
    )
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
        }
    )
    assert result == {}


def test_named_allowlist_wins_over_a_write_capable_tools_list(tmp_path, monkeypatch):
    agents_dir = _agents_dir(tmp_path)
    _write_agent_def(agents_dir, "premise-checker", '["Read", "Grep", "Bash", "Write"]')
    _patch_agents_dir(monkeypatch, agents_dir)

    script = tmp_path / "hand.workflow.mjs"
    script.write_text(
        'agent({ prompt: "check", agentType: "premise-checker" });\n', encoding="utf-8"
    )
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
        }
    )
    assert result == {}


def test_unresolvable_agent_definition_falls_back_to_named_allowlist(tmp_path, monkeypatch):
    import coordinator_core.hooks.block_workflow_unmodeled_agent as bwua

    monkeypatch.setattr(bwua, "_agents_dir", lambda: None)

    script = tmp_path / "hand.workflow.mjs"
    script.write_text(
        'agent({ prompt: "check", agentType: "premise-checker" });\n', encoding="utf-8"
    )
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
        }
    )
    assert result == {}


def test_unresolvable_agent_definition_not_on_allowlist_stays_denied(tmp_path, monkeypatch):
    import coordinator_core.hooks.block_workflow_unmodeled_agent as bwua

    monkeypatch.setattr(bwua, "_agents_dir", lambda: None)

    script = tmp_path / "hand.workflow.mjs"
    script.write_text(
        'agent({ prompt: "do it", agentType: "executor" });\n', encoding="utf-8"
    )
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_inline_write_capable_script_no_receipt_possible_is_denied(tmp_path):
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": 'agent({ prompt: "do it", agentType: "executor" });'},
            "cwd": str(tmp_path),
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "inline script" in hso["permissionDecisionReason"]


def test_inline_read_only_fanout_is_exempt(tmp_path, monkeypatch):
    agents_dir = _agents_dir(tmp_path)
    _write_agent_def(agents_dir, "premise-checker", "[Read, Grep]")
    _patch_agents_dir(monkeypatch, agents_dir)

    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {
                "script": 'agent({ prompt: "check", agentType: "premise-checker" });'
            },
            "cwd": str(tmp_path),
        }
    )
    assert result == {}


def test_no_agent_calls_at_all_is_vacuously_exempt(tmp_path):
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


def test_emit_wave_fire_receipt_shape_is_sanctioned(tmp_path):
    import hashlib
    import json

    script = tmp_path / "fire-0-1.mjs"
    script.write_bytes(b'agent({ prompt: "do it", agentType: "executor" });\n')
    actual_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    receipt = script.with_name(script.name + ".emitted.json")
    receipt.write_text(
        json.dumps({"sha256": actual_sha, "session_id": "s1", "plan": None}),
        encoding="utf-8",
    )
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
            "session_id": "s1",
        }
    )
    assert result == {}


_WRITE_CAPABLE_CALL = 'agent({ prompt: "do it", agentType: "executor" });\n'


def _fire(tmp_path: Path, script: Path) -> dict:
    return bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
        }
    )


def _write_script(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_WRITE_CAPABLE_CALL, encoding="utf-8")
    return path


def test_pinned_distill_harvest_without_receipt_draws_no_advisory(tmp_path, monkeypatch):
    agents_dir = _agents_dir(tmp_path)
    _write_agent_def(agents_dir, "executor", "[Read, Edit, Write, Bash]")
    _patch_agents_dir(monkeypatch, agents_dir)
    script = _write_script(
        tmp_path / "pipelines" / "artifact-distillation" / "distill-harvest.workflow.js"
    )
    assert _fire(tmp_path, script) == {}


def test_same_named_script_outside_the_pinned_dir_is_still_denied(tmp_path):
    script = _write_script(tmp_path / "elsewhere" / "distill-harvest.workflow.js")
    hso = _fire(tmp_path, script)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "no receipt" in hso["permissionDecisionReason"]


def test_pinned_dir_with_a_different_basename_is_still_denied(tmp_path):
    script = _write_script(
        tmp_path / "pipelines" / "artifact-distillation" / "other.workflow.js"
    )
    hso = _fire(tmp_path, script)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_pinned_script_with_a_stale_receipt_keeps_the_sha_check(tmp_path):
    import json

    script = _write_script(
        tmp_path / "pipelines" / "artifact-distillation" / "distill-harvest.workflow.js"
    )
    script.with_name(script.name + ".emitted.json").write_text(
        json.dumps({"sha256": "0" * 64, "session_id": "s1"}), encoding="utf-8"
    )
    hso = _fire(tmp_path, script)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "changed after emission" in hso["permissionDecisionReason"]


def _assert_names_both_doors(result) -> None:
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    assert "--pipeline <name> --brief" in reason
    assert "--plan <plan-path>" in reason


def test_no_receipt_deny_names_pipeline_route_for_script_path(tmp_path):
    script = tmp_path / "hand.workflow.mjs"
    script.write_text('agent({ prompt: "do it" });\n', encoding="utf-8")
    _assert_names_both_doors(
        bwfe._handler(
            {
                "tool_name": "Workflow",
                "tool_input": {"scriptPath": str(script)},
                "cwd": str(tmp_path),
            }
        )
    )


@pytest.mark.parametrize("name", ["fire-0-1.mjs", "fire-3.mjs", "repair-fire-2.mjs"])
def test_receiptless_wave_fire_is_not_called_hand_rolled(tmp_path, name):
    script = tmp_path / name
    script.write_text('agent({ prompt: "do it" });\n', encoding="utf-8")
    result = bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(tmp_path),
        }
    )
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    assert "emit-wave-fire" in reason
    assert "hand-rolled" not in reason


def test_no_receipt_deny_names_pipeline_route_for_inline_script():
    _assert_names_both_doors(
        bwfe._handler(
            {
                "tool_name": "Workflow",
                "tool_input": {"script": 'agent({ prompt: "do it" });\n'},
            }
        )
    )
