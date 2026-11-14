"""Ref-only merge: update-ref of the checked-out branch to a merge commit, no `read-tree -m -u`, is refused for every caller."""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core import deny_registry, machine_profile
from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.guard_messages.testing import envelope_of

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo, *args):
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=_ENV,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "base.txt").write_text("base\n")
    _git(root, "add", "base.txt")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "other")
    (root / "theirs.txt").write_text("theirs\n")
    _git(root, "add", "theirs.txt")
    _git(root, "commit", "-q", "-m", "theirs")
    _git(root, "checkout", "-q", "main")
    (root / "ours.txt").write_text("ours\n")
    _git(root, "add", "ours.txt")
    _git(root, "commit", "-q", "-m", "ours")
    a = _git(root, "rev-parse", "main")
    b = _git(root, "rev-parse", "other")
    merge = _git(root, "commit-tree", "-p", a, "-p", b, "-m", "merge", _git(root, "merge-tree", "--write-tree", a, b))
    return root, a, b, merge


def _payload(command, cwd, subagent):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "s1", "cwd": str(cwd)}
    if subagent:
        payload.update({"agent_id": "deadbeef0123", "agent_type": "coordinator:executor"})
    return payload


def _run(command, cwd, subagent):
    return envelope_of(guard.check(_payload(command, cwd, subagent)))


def _reason(env):
    return env["hookSpecificOutput"]["permissionDecisionReason"]


def _round_four(a, b, tree="$T"):
    return (
        f"T=$(git write-tree); M=$(git commit-tree {tree} -p {a} -p {b} -m merge); "
        f"git update-ref refs/heads/main $M {a}"
    )


@pytest.mark.parametrize("subagent", [False, True], ids=["em", "subagent"])
def test_round_four_merge_is_refused_and_names_the_engine_route(repo, subagent):
    root, a, b, _merge = repo
    env = _run(_round_four(a, b), root, subagent)
    assert env is not None
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "dispatch.commit_merge" in _reason(env)


@pytest.mark.parametrize("subagent", [False, True], ids=["em", "subagent"])
def test_literal_merge_commit_is_refused_through_head_or_branch_ref(repo, subagent):
    root, a, _b, merge = repo
    for ref in ("refs/heads/main", "HEAD"):
        assert _run(f"git update-ref {ref} {merge} {a}", root, subagent) is not None, ref


def test_em_refusal_survives_the_guard_level_policy(repo, monkeypatch):
    root, a, b, _merge = repo
    env = _run(_round_four(a, b), root, False)
    assert deny_registry.guard_deny_class("block-subagent-destructive-action") == deny_registry.BOX_DEGRADING
    for level in ("strict", "warn", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        machine_profile.reset_cache()
        assert machine_profile.apply_guard_level("block-subagent-destructive-action", env) is env, level


@pytest.mark.parametrize("subagent", [False, True], ids=["em", "subagent"])
@pytest.mark.parametrize("sync", ["git read-tree -m -u {a} {m}", "git read-tree -mu {a} {m}"])
def test_read_tree_sync_earlier_in_the_command_is_allowed(repo, subagent, sync):
    root, a, _b, merge = repo
    sync_cmd = sync.format(a=a, m=merge)
    env = _run(f"{sync_cmd} && git update-ref refs/heads/main {merge} {a}", root, subagent)
    # a subagent's own `read-tree -u` is a separate deny of this guard, not this predicate
    assert env is None or guard._REF_ONLY_MERGE_DENY_KIND not in _reason(env)
    assert env is None or subagent


def test_read_tree_after_the_update_ref_does_not_count(repo):
    root, a, _b, merge = repo
    env = _run(f"git update-ref refs/heads/main {merge} {a} && git read-tree -m -u {a} {merge}", root, False)
    assert env is not None
    assert guard._REF_ONLY_MERGE_DENY_KIND in _reason(env)


@pytest.mark.parametrize("subagent", [False, True], ids=["em", "subagent"])
def test_single_parent_update_ref_of_the_checked_out_branch_is_allowed(repo, subagent):
    root, a, _b, _merge = repo
    parent = _git(root, "rev-parse", f"{a}^")
    assert _run(f"git update-ref refs/heads/main {parent} {a}", root, subagent) is None
    one_parent = "T=$(git write-tree); M=$(git commit-tree $T -p " + a + " -m x); git update-ref refs/heads/main $M " + a
    assert _run(one_parent, root, subagent) is None


@pytest.mark.parametrize("subagent", [False, True], ids=["em", "subagent"])
def test_merge_update_ref_of_a_branch_not_checked_out_is_allowed(repo, subagent):
    root, a, b, merge = repo
    assert _run(f"git update-ref refs/heads/other {merge} {b}", root, subagent) is None
    built = _round_four(a, b).replace("refs/heads/main", "refs/heads/other").replace(f"$M {a}", f"$M {b}")
    assert _run(built, root, subagent) is None


@pytest.mark.parametrize("subagent", [False, True], ids=["em", "subagent"])
def test_engine_commit_merge_route_is_allowed(repo, subagent):
    root, _a, _b, _merge = repo
    command = (
        f"coordinator-invoke dispatch.commit_merge --repo {root} "
        """'{"theirs_ref":"other","resolved_dir":"x","conflicted":[],"message":"m","expected_head":"abc"}'"""
    )
    assert _run(command, root, subagent) is None


def test_non_checked_out_branch_costs_no_git_spawn(repo, monkeypatch):
    root, a, _b, merge = repo
    import coordinator_core.git.run as git_run

    def boom(*args, **kwargs):
        raise AssertionError("spawned git for a ref that is not the checked-out branch")

    monkeypatch.setattr(git_run, "run_git", boom)
    assert _run(f"git update-ref refs/heads/other {merge} {a}", root, False) is None
