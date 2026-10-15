from __future__ import annotations

from coordinator_core.write_guards import block_em_strict_dispatch_code_write as guard


def test_no_sizing_refusal_names_ask_form_and_target(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: str(tmp_path))
    monkeypatch.setattr(guard, "_is_subagent_session", lambda sid, root: False)
    monkeypatch.setattr(guard, "_session_touched_sizing_files", lambda sid, root: [])
    out = guard.check(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "src" / "thing.py"), "content": "x"},
            "cwd": str(tmp_path),
            "session_id": "sess-12345678",
        }
    )
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "args: {ask:" in reason
    assert "thing.py" in reason
    assert "sizing:" not in reason
