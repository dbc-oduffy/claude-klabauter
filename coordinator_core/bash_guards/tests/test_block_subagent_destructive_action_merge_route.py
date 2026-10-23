"""Executor guard: merge-tree is read-only-allowed; a main-tree `git merge` deny names the engine stage op."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.guard_messages.testing import envelope_of

_STAGE = """coordinator-invoke branch.merge_into_workstream --repo <repo-root> '{"branch":"<ref>","reason":"<why>","stage":true}'"""


@pytest.fixture
def trees(tmp_path):
    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    clone = main / "scratch" / "merge-clone"
    (clone / ".git").mkdir(parents=True)
    return main, clone


def _run(command, cwd):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
        "cwd": str(cwd),
    }
    return envelope_of(guard.check(payload))


@pytest.mark.parametrize(
    "command",
    ["git merge-tree --write-tree HEAD other", "git merge-tree --write-tree --name-only HEAD other"],
)
def test_merge_tree_is_allowed(trees, command):
    assert _run(command, trees[0]) is None


@pytest.mark.parametrize("command", ["git merge other", "git merge --no-commit other"])
def test_main_tree_merge_denied_and_names_the_stage_op(trees, command):
    out = _run(command, trees[0])
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git merge (not --ff-only)" in reason
    assert _STAGE in reason


def test_deny_record_carries_the_adaptable_stage_template():
    record = guard._build_record("git merge (not --ff-only)", "a", "coordinator:executor", "git merge x", "git merge x")
    templates = [getattr(m, "template", None) for m in record.members]
    assert _STAGE in templates


def test_scratch_clone_merge_and_ff_only_still_allowed(trees):
    main, clone = trees
    assert _run("git merge --no-ff origin/feature", clone) is None
    assert _run("git merge --ff-only x", main) is None
