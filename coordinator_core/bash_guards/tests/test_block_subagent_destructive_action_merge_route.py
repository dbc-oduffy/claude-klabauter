"""Executor guard: merge-tree is read-only-allowed; a main-tree `git merge` deny names the engine stage op."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.guard_messages.testing import envelope_of
from pathlib import Path

_STAGE = """coordinator-invoke branch.merge_into_workstream --repo <repo-root> '{"branch":"<ref>","reason":"<why>","stage":true}'"""


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
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
def test_merge_tree_is_allowed(trees, command) -> None:
    assert _run(command, trees[0]) is None


@pytest.mark.parametrize("command", ["git merge other", "git merge --no-commit other"])
def test_main_tree_merge_denied_and_names_the_stage_op(trees, command) -> None:
    out = _run(command, trees[0])
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git merge (not --ff-only)" in reason
    assert _STAGE in reason


def test_deny_record_carries_the_adaptable_stage_template() -> None:
    record = guard._build_record("git merge (not --ff-only)", "a", "coordinator:executor", "git merge x", "git merge x")
    templates = [getattr(m, "template", None) for m in record.members]
    assert _STAGE in templates


def test_scratch_clone_merge_and_ff_only_still_allowed(trees) -> None:
    main, clone = trees
    assert _run("git merge --no-ff origin/feature", clone) is None
    assert _run("git merge --ff-only x", main) is None


def _c(path) -> str:
    return str(path).replace("\\", "/")


def test_dash_c_into_isolated_nested_clone_merge_is_allowed(trees) -> None:
    main, clone = trees
    assert _run(f"git -C {_c(clone)} merge --no-ff shared/tip", main) is None
    assert _run(f"git -C {_c(clone)} merge --no-ff shared/tip", clone.parent.parent.parent) is None


def test_dash_c_relative_nested_clone_merge_is_allowed(trees) -> None:
    main, _clone = trees
    assert _run("git -C scratch/merge-clone merge --no-ff shared/tip", main) is None


def test_dash_c_clone_does_not_cover_a_bare_merge_in_the_same_command(trees) -> None:
    main, clone = trees
    out = _run(f"git -C {_c(clone)} merge a && git merge b", main)
    assert out is not None


def test_dash_c_to_shared_repo_root_or_subdir_merge_stays_denied(trees) -> None:
    main, _clone = trees
    (main / "sub").mkdir()
    for target in (_c(main), _c(main / "sub"), _c(main / "scratch")):
        out = _run(f"git -C {target} merge --no-ff shared/tip", main)
        assert out is not None, target
        assert "git merge (not --ff-only)" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_shared_repo_merge_verdict_unchanged_without_dash_c(trees) -> None:
    out = _run("git merge --no-ff shared/tip", trees[0])
    assert out is not None
    assert _STAGE in out["hookSpecificOutput"]["permissionDecisionReason"]


def _on(main: Path, branch: str) -> None:
    (main / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")


@pytest.mark.parametrize("branch", ["example-operator/machine-a/feature-x", "feature/x", "wip"])
def test_shared_tree_merge_on_a_non_trunk_branch_is_allowed(trees, branch) -> None:
    _on(trees[0], branch)
    assert _run("git merge --no-ff shared/tip", trees[0]) is None
    assert _run("git merge other", trees[0]) is None


@pytest.mark.parametrize("branch", ["main", "master", "release/2026.11", "release/x/y"])
def test_shared_tree_merge_on_trunk_or_release_stays_denied(trees, branch) -> None:
    _on(trees[0], branch)
    out = _run("git merge --no-ff shared/tip", trees[0])
    assert out is not None
    assert "git merge (not --ff-only)" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_merge_with_detached_head_fails_closed(trees) -> None:
    (trees[0] / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    assert _run("git merge other", trees[0]) is not None


@pytest.fixture
def other_repo(tmp_path: Path) -> Path:
    other = tmp_path / "other-repo"
    (other / ".git").mkdir(parents=True)
    return other


@pytest.mark.parametrize("branch", ["main", "feature/x"])
def test_leading_cd_into_another_repo_merges_like_dash_c(trees, other_repo, branch) -> None:
    _on(other_repo, branch)
    assert _run(f"git -C {_c(other_repo)} merge --no-ff x", trees[0]) is None
    assert _run(f"cd {_c(other_repo)} && git merge --no-ff x", trees[0]) is None


def test_leading_cd_into_session_repo_on_main_stays_denied(trees) -> None:
    main, clone = trees
    _on(main, "main")
    for cwd in (main, clone):
        out = _run(f"cd {_c(main)} && git merge other", cwd)
        assert out is not None, cwd
        assert "git merge (not --ff-only)" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_leading_cd_into_another_repo_does_not_cover_a_second_cd(trees, other_repo) -> None:
    main, _clone = trees
    _on(main, "main")
    assert _run(f"cd {_c(other_repo)} && git merge a && cd {_c(main)} && git merge b", main) is not None


def test_dash_c_shared_root_on_main_stays_denied_even_if_cwd_branch_is_feature(trees) -> None:
    main, _clone = trees
    _on(main, "main")
    assert _run(f"git -C {_c(main)} merge other", main) is not None
