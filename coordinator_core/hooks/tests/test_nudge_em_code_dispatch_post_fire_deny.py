"""Post-fire deny of `nudge_em_code_dispatch.op`: floor-guard, every tool, ahead of the early returns."""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.machine_profile as mp
from coordinator_core.hooks import _em_write_budget as leaf
from coordinator_core.hooks import flag_em_poll_in_flight as fl
from coordinator_core.hooks import nudge_em_code_dispatch as mod

SID = "sess-postfire"


@pytest.fixture
def root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / "scratch").mkdir()
    (repo / "docs").mkdir()
    for target in (mod, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    monkeypatch.setattr(mp, "guard_level", lambda name: "warn")
    return repo


def _fire(monkeypatch, runs):
    monkeypatch.setattr(fl, "session_workflow_run_ids", lambda sid: runs)


def _payload(root: Path, tool: str, rel: str, **extra) -> dict:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    tool_input = {key: str(root / rel), "old_string": "a = 1\n", "new_string": "a = f(1)\n"}
    tool_input.update(extra)
    return {"tool_name": tool, "session_id": SID, "cwd": str(root), "tool_input": tool_input}


def _is_deny(result) -> bool:
    return bool(result) and result["hookSpecificOutput"].get("permissionDecision") == "deny"


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_fired_product_write_is_denied_at_warn(root, monkeypatch, tool):
    _fire(monkeypatch, ["run-1"])
    result = mod.op(_payload(root, tool, "pkg/mod.py"))
    assert _is_deny(result)
    assert "workflow.revise" in result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("rel", ["CLAUDE.md", "sub/CLAUDE.md", ".claude/CLAUDE.md"])
def test_fired_claude_md_write_names_the_grant_route(root, monkeypatch, rel):
    from coordinator_core.session.claude_md_grant import GRANT_CLI_NAME

    _fire(monkeypatch, ["run-1"])
    result = mod.op(_payload(root, "Edit", rel))
    assert _is_deny(result)
    text = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert GRANT_CLI_NAME in text and "workflow.revise" in text and "already fired engine-ask" in text
    assert "from the repo that holds the CLAUDE.md" in text


def test_multiedit_edits_path_is_denied(root, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    payload = {
        "tool_name": "MultiEdit",
        "session_id": SID,
        "cwd": str(root),
        "tool_input": {"edits": [{"file_path": str(root / "pkg/mod.py"), "old_string": "a", "new_string": "b"}]},
    }
    assert _is_deny(mod.op(payload))


def test_fired_edit_of_tracked_markdown_is_denied(root, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    assert _is_deny(mod.op(_payload(root, "Edit", "docs/readme.md")))


def test_fired_edit_with_semantic_bypass_shape_is_denied(root, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    payload = _payload(root, "Edit", "pkg/mod.py", old_string="a = 1\n", new_string="a  =  1\n")
    assert _is_deny(mod.op(payload))


@pytest.mark.parametrize("rel", ["state/handoffs/h.md", "scratch/probe.txt", ".coordinator-local/x/y.md"])
def test_fired_non_product_paths_are_not_denied(root, monkeypatch, rel):
    _fire(monkeypatch, ["run-1"])
    assert not _is_deny(mod.op(_payload(root, "Write", rel, content="x")))


def test_path_outside_the_repo_is_not_denied(root, monkeypatch, tmp_path):
    _fire(monkeypatch, ["run-1"])
    payload = _payload(root, "Edit", "pkg/mod.py")
    payload["tool_input"]["file_path"] = str(tmp_path / "elsewhere" / "mod.py")
    assert not _is_deny(mod.op(payload))


def test_subagent_is_unaffected(root, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    payload = _payload(root, "Edit", "pkg/mod.py")
    payload["agent_id"] = "agent-1"
    assert mod.op(payload) is None


def test_unfired_session_gets_no_post_fire_deny(root, monkeypatch):
    _fire(monkeypatch, [])
    monkeypatch.setattr(mod, "_session_sizing_state", lambda payload, sid: (True, False))
    monkeypatch.setattr(leaf, "_session_sizing_state", lambda payload, sid: (True, False))
    result = mod.op(_payload(root, "Edit", "pkg/mod.py"))
    assert "already fired engine-ask" not in str(result)


def test_post_fire_deny_without_paths_decides_on_the_fire_alone(monkeypatch):
    _fire(monkeypatch, ["run-1"])
    assert _is_deny(mod._post_fire_deny(SID))
    _fire(monkeypatch, [])
    assert mod._post_fire_deny(SID) is None
