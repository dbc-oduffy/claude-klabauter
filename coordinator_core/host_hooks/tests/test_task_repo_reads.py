"""A grok task repo cannot pull EM doctrine into the prompt."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from coordinator_core.host_hooks.contract import HostEvent
from coordinator_core.host_hooks.grok_adapter import GrokAdapter
from coordinator_core.host_hooks.task_repo_reads import match


def _event(tmp_path, tool, payload, cwd):
    return HostEvent(host="grok", event="PreToolUse", session_id="s", tool_name=tool,
                     tool_input=payload, cwd=str(cwd))


def test_task_repo_denies_an_outside_read_and_renders_it(tmp_path):
    repo = tmp_path / "task"
    (repo / ".git").mkdir(parents=True)
    (repo / "pkg").mkdir()
    inside = repo / "pkg" / "collapse.py"
    inside.write_text("def collapse_ws(text):\n    return text\n", encoding="utf-8")
    outside = tmp_path / "skills" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("engine_ask.submit\n" * 400, encoding="utf-8")
    denied = match(_event(tmp_path, "read_file", {"target_file": str(outside)}, repo))
    assert denied["permissionDecision"] == "deny"
    assert "coordinator-invoke" not in denied["permissionDecisionReason"]
    rendered = GrokAdapter().render(
        _event(tmp_path, "read_file", {"target_file": str(outside)}, repo), denied)
    assert rendered["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert match(_event(tmp_path, "read_file", {"target_file": str(inside)}, repo)) is None
    assert match(_event(tmp_path, "grep", {"path": str(outside.parent), "pattern": "WORKER"}, repo))["permissionDecision"] == "deny"


def test_unresolved_tmp_cwd_allows_an_in_repo_read(monkeypatch) -> None:
    """The grok hook entry allows `pkg/a.py` when cwd is unresolved `/tmp/<repo>`."""
    from coordinator_core.host_hooks import shim

    repo = Path("/tmp") / f"task-repo-read-{uuid.uuid4().hex}"
    outside = Path("/tmp") / f"task-repo-outside-{uuid.uuid4().hex}"
    try:
        (repo / ".git").mkdir(parents=True)
        (repo / "pkg").mkdir()
        (repo / "pkg" / "a.py").write_text("x\n", encoding="utf-8")
        outside.write_text("doctrine\n", encoding="utf-8")
        assert str(repo).startswith("/tmp/")
        assert repo.resolve() != repo
        dials = {"n": 0}

        def fake_dial(request):
            dials["n"] += 1
            return {"result": {}}

        monkeypatch.setattr(shim, "_dial", fake_dial)
        allowed = shim.decide("grok", {
            "hook_event_name": "PreToolUse",
            "session_id": "s",
            "cwd": str(repo),
            "tool_name": "read_file",
            "tool_input": {"target_file": "pkg/a.py"},
        })
        decision = (allowed.get("hookSpecificOutput") or {}).get("permissionDecision")
        assert decision != "deny"
        assert dials["n"] == 1
        denied = shim.decide("grok", {
            "hook_event_name": "PreToolUse",
            "session_id": "s",
            "cwd": str(repo),
            "tool_name": "grep",
            "tool_input": {"path": str(outside), "pattern": "x"},
        })
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert dials["n"] == 1
    finally:
        shutil.rmtree(repo, ignore_errors=True)
        outside.unlink(missing_ok=True)


def test_fleet_source_repo_still_reads_outside(tmp_path):
    repo = tmp_path / "DoE"
    (repo / ".git").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("doctrine\n", encoding="utf-8")
    outside = tmp_path / "skills" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("x\n", encoding="utf-8")
    assert match(_event(tmp_path, "read_file", {"target_file": str(outside)}, repo)) is None
