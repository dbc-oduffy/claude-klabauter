"""scratch_clone_failures with an injected git runner: UNREADABLE on rc != 0 or a raised OSError,
[] for a clean repo."""

from __future__ import annotations

from coordinator_core.bash_guards._unborn_scratch_repo import UNREADABLE, scratch_clone_failures


def _scratch_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    clone = tmp_path / "scratch" / "c"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return str(clone)


def test_nonzero_rc_is_unreadable(tmp_path):
    root = _scratch_repo(tmp_path)
    assert scratch_clone_failures(root, lambda args, cwd: (128, "")) == [UNREADABLE]


def test_raised_oserror_is_unreadable(tmp_path):
    root = _scratch_repo(tmp_path)

    def boom(args, cwd):
        raise OSError("spawn failed")

    assert scratch_clone_failures(root, boom) == [UNREADABLE]


def test_clean_repo_has_no_failures(tmp_path):
    root = _scratch_repo(tmp_path)
    assert scratch_clone_failures(root, lambda args, cwd: (0, "")) == []


def _clone_verdict(cwd):
    from coordinator_core.bash_guards import block_subagent_destructive_action as guard

    result = guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git clone https://example.invalid/x.git /elsewhere/x"},
            "session_id": "sess1",
            "cwd": str(cwd),
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:queue-grind-op-runner",
        }
    )
    return None if result is None else result.summary


def test_clone_denial_outside_any_repo_names_no_placeholder(tmp_path):
    verdict = _clone_verdict(tmp_path)
    assert verdict is not None
    assert "<repo>" not in verdict
    assert "scratchpad" in verdict


def test_clone_denial_inside_repo_names_resolved_scratch(tmp_path):
    (tmp_path / ".git").mkdir()
    verdict = _clone_verdict(tmp_path)
    assert verdict is not None
    assert "<repo>" not in verdict
    assert (tmp_path.resolve() / "scratch").as_posix() in verdict
