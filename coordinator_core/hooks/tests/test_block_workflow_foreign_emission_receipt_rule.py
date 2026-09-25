"""coordinator_core/hooks/tests/test_block_workflow_foreign_emission_receipt_rule.py

Subject: `coordinator_core.hooks.block_workflow_foreign_emission`'s § Read-only
exemption / no-receipt deny, added per issue #87 item 3 (PM ruling
2026-09-25): a hand-rolled Workflow that dispatches at least one write-capable
(or untyped) `agent()` call and carries no verifying receipt is now DENIED,
not silently let through — unless every `agent()` call resolves to a
read-only `agentType`.
"""

from __future__ import annotations

from pathlib import Path

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
    """The real `premise-checker` definition carries `Write`/`Bash` in its
    `tools:` list (scratch-file/probe use), but PM ruling 2026-09-25 names it
    read-only for this purpose -- the allowlist must win over the raw
    inventory, not the other way round."""
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
    """Pinned as `test_bwfe_no_receipt_no_advisory` already asserts — no
    dispatch means no cost/write risk this guard cares about."""
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
    """The receipt shape is producer-agnostic — a receipt written by
    emit-wave-fire (same keys as emit-dispatch-workflow's) verifies and
    silences this guard identically. See `emit-wave-fire.py::_write_fire_receipt`."""
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
