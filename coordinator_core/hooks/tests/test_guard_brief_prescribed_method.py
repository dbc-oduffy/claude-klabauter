"""Pins for `guard_brief_prescribed_method.evaluate`: lint a brief against the route table."""

from __future__ import annotations

import inspect
import json

import pytest

from coordinator_core.hooks import guard_brief_prescribed_method as g

_PATTERN = r"\bue_\w+_job\.py\b"
_BRIEF = "run ue_build_job.py to import the meshes"


def _table(root, level="warn", lint=True):
    data = {
        "schema_version": 1,
        "default_agent_type": "coordinator:executor",
        "work_classes": [
            {
                "id": "ue",
                "match": {"keywords": ["unreal"]},
                "agent_type": "example-game-repo-control:ue-asset",
                "on_mismatch": "refuse",
            }
        ],
    }
    if lint:
        data["brief_lint"] = {"prescribed_method_patterns": [_PATTERN], "level": level}
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "route-table.json").write_text(json.dumps(data), encoding="utf-8")


def _agent(prompt):
    return {"tool_name": "Agent", "tool_input": {"prompt": prompt}}


def _hso(result):
    return result["hookSpecificOutput"]


def test_signature_is_pinned():
    sig = inspect.signature(g.evaluate)
    assert list(sig.parameters) == ["payload", "env", "repo_root"]


def test_no_table_admits(tmp_path):
    assert g.evaluate(_agent(_BRIEF), repo_root=tmp_path) == {}


def test_table_without_brief_lint_admits(tmp_path):
    _table(tmp_path, lint=False)
    assert g.evaluate(_agent(_BRIEF), repo_root=tmp_path) == {}


def test_warn_returns_advisory_naming_span(tmp_path):
    _table(tmp_path, "warn")
    hso = _hso(g.evaluate(_agent(_BRIEF), repo_root=tmp_path))
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    assert "ue_build_job.py" in hso["additionalContext"]
    assert "State the outcome" in hso["additionalContext"]


def test_strict_denies(tmp_path):
    _table(tmp_path, "strict")
    hso = _hso(g.evaluate(_agent(_BRIEF), repo_root=tmp_path))
    assert hso["permissionDecision"] == "deny"
    assert "ue_build_job.py" in hso["permissionDecisionReason"]


def test_strict_deny_passes_through_guard_level(tmp_path, monkeypatch):
    _table(tmp_path, "strict")
    seen = []
    monkeypatch.setattr(g, "apply_guard_level", lambda name, env: seen.append(name) or None)
    assert g.evaluate(_agent(_BRIEF), repo_root=tmp_path) == {}
    assert seen == ["guard-brief-prescribed-method"]


def test_outcome_brief_admits(tmp_path):
    _table(tmp_path, "strict")
    assert g.evaluate(_agent("import the meshes into the level"), repo_root=tmp_path) == {}


def test_workflow_inline_script_is_linted(tmp_path):
    _table(tmp_path, "warn")
    payload = {"tool_name": "Workflow", "tool_input": {"script": f'agent({{prompt: "{_BRIEF}"}})'}}
    assert _hso(g.evaluate(payload, repo_root=tmp_path))["permissionDecision"] == "allow"


def test_workflow_script_path_is_not_linted(tmp_path):
    _table(tmp_path, "strict")
    script = tmp_path / "s.mjs"
    script.write_text(_BRIEF, encoding="utf-8")
    payload = {"tool_name": "Workflow", "tool_input": {"scriptPath": str(script)}}
    assert g.evaluate(payload, repo_root=tmp_path) == {}


def test_other_tool_admits(tmp_path):
    _table(tmp_path, "strict")
    payload = {"tool_name": "Bash", "tool_input": {"prompt": _BRIEF}}
    assert g.evaluate(payload, repo_root=tmp_path) == {}


def test_span_is_truncated(tmp_path):
    (tmp_path / ".claude").mkdir()
    data = {
        "schema_version": 1,
        "default_agent_type": "coordinator:executor",
        "work_classes": [
            {"id": "c", "match": {"keywords": ["k"]}, "agent_type": "a:b", "on_mismatch": "refuse"}
        ],
        "brief_lint": {"prescribed_method_patterns": ["x{200}"], "level": "warn"},
    }
    (tmp_path / ".claude" / "route-table.json").write_text(json.dumps(data), encoding="utf-8")
    ctx = _hso(g.evaluate(_agent("x" * 200), repo_root=tmp_path))["additionalContext"]
    assert "..." in ctx and "x" * 81 not in ctx


def test_cwd_walk_up_finds_table(tmp_path):
    _table(tmp_path, "warn")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    payload = {**_agent(_BRIEF), "cwd": str(sub)}
    assert _hso(g.evaluate(payload))["permissionDecision"] == "allow"


def test_malformed_table_fails_open(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "route-table.json").write_text("{nope", encoding="utf-8")
    assert g.evaluate(_agent(_BRIEF), repo_root=tmp_path) == {}


def test_garbage_payload_fails_open():
    assert g.evaluate({"tool_name": "Agent", "tool_input": "x"}) == {}


def test_handler_delegates(tmp_path):
    _table(tmp_path, "warn")
    out = g._handler({**_agent(_BRIEF), "env": {}}, repo_root=tmp_path)
    assert _hso(out)["permissionDecision"] == "allow"
