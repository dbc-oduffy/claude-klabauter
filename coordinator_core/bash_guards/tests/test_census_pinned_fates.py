"""Pinned guard fates from the 2026-10-28 denial census: each shape is guided or allowed, and the
kept blocks still refuse and name a route that does not itself refuse.

Registered guards (``deny_registry``) deny only their degrading shape; every other guard resolves
to a log-only advisory through ``machine_profile.apply_guard_level``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core import deny_registry, machine_profile
from coordinator_core.bash_guards import (
    block_approval_sentinel_creation,
    block_subagent_destructive_action as sub_guard,
    check_test_suite_invocation,
    dispatch,
    guard_em_post_fire_shell_write,
)
from coordinator_core.bash_guards import bump_foreign_repo_write as bump
from coordinator_core.bash_guards.tests.test_bump_foreign_repo_write import (
    _posix,
    _reset_git_root_cache,  # noqa: F401 -- autouse fixture reused
    _set_anchor,
    repos,  # noqa: F401 -- reused fixture
)
from coordinator_core.bash_guards.tests.test_bump_outside_repo_write import (
    _clean_bump_env,  # noqa: F401 -- reused fixture
)
from coordinator_core.guard_messages.testing import envelope_of

pytestmark = pytest.mark.spawns_process

SUB_ID = "a1b2c3d4e5f6a7b8"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("a")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "init")
    (root / "scratch").mkdir()
    return root


def _payload(cmd, cwd, *, subagent=False, session="sess-fates"):
    p = {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": session, "cwd": str(cwd)}
    if subagent:
        p["agent_id"] = SUB_ID
        p["agent_type"] = "coordinator:executor"
    return p


def _denies(result) -> bool:
    items = result if isinstance(result, list) else [result]
    return any(
        isinstance(r, dict) and r.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        for r in items
    )


def _sub_check(cmd, cwd):
    """The subagent guard's verdict after the policy point: the deny envelope, or None."""
    record = sub_guard.check(_payload(cmd, cwd, subagent=True))
    return machine_profile.apply_guard_level(
        "block-subagent-destructive-action", envelope_of(record), subagent=True
    )


def _reason(envelope) -> str:
    assert envelope is not None, "expected a deny, got allow"
    return envelope["hookSpecificOutput"]["permissionDecisionReason"]


# --- block_subagent_destructive_action: regenerable build/cache dirs are guide-only ---------------


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf .next",
        "rm -rf .atlas-builds",
        "rm -rf .ddct/pglite-pre-20261028",
        "rm -rf raw_sources/image-hashes-cache",
        "rm -rf .next .atlas-builds .ddct/pglite-pre-1 raw_sources/image-hashes-cache",
        "cd app && rm -rf .next",
    ],
)
def test_subagent_rm_of_regenerable_dir_is_not_denied(repo, cmd):
    assert _sub_check(cmd, repo) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf src",
        "rm -rf .next src",
        "rm -rf .next/../src",
        "rm -rf $HOME/.next",
        "rm -rf .ddct",
        "echo .next | xargs rm -rf",
    ],
)
def test_subagent_rm_of_anything_else_still_denies_and_names_the_trash_op(repo, cmd):
    reason = _reason(_sub_check(cmd, repo))
    assert "run.paths_trash" in reason
    assert "workflow.revise" not in reason and "EM runs the deletes" not in reason


# --- guard_em_post_fire_shell_write: never denies the EM's own act ---------------------------------


@pytest.fixture()
def fired(monkeypatch):
    from coordinator_core.hooks import _em_post_fire

    monkeypatch.setattr(_em_post_fire, "fired", lambda _sid: True)
    return _em_post_fire


def test_post_engine_ask_lock_is_unregistered_so_it_never_denies():
    assert deny_registry.guard_deny_class("block-em-post-fire-product-write") is None
    assert deny_registry.guard_deny_class("guard-em-post-fire-shell-write") is None


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf .next",
        "rm -rf .atlas-builds .ddct/pglite-pre-1",
        "git add new_one.py new_two.py",
        "cp a.txt {sp}/copy.txt",
        "mkdir -p {sp}/tree && cp -R . {sp}/tree",
    ],
)
def test_em_own_act_after_engine_ask_is_never_denied(repo, fired, tmp_path, cmd):
    sp = tmp_path / "scratchpad"
    sp.mkdir()
    cmd = cmd.format(sp=sp)
    assert guard_em_post_fire_shell_write.check(cmd, "sess-fates", str(repo), {"cwd": str(repo)}) is None
    assert not _denies(fired.deny_envelope("sess-fates", str(repo)))
    assert not _denies(dispatch.evaluate_payload_json(json.dumps(_payload(cmd, repo))))


def test_neither_leg_routes_the_delete_to_the_other(repo):
    sub_reason = _reason(_sub_check("rm -rf src", repo))
    assert "workflow.revise" not in sub_reason
    assert "run.paths_trash" in sub_reason
    em = dispatch.evaluate_payload_json(json.dumps(_payload("rm -rf .next", repo)))
    assert not _denies(em)
    assert "EM runs the deletes" not in json.dumps(em)


# --- block_subagent_destructive_action: the clone remedy must not refuse itself -------------------


def test_clone_into_repo_scratch_from_the_repo_cwd_passes(repo):
    assert _sub_check(f"git clone {repo} scratch/head-clone-73", repo) is None


def test_clone_after_one_leading_cd_into_repo_scratch_passes(repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _sub_check(f"cd {repo} && git clone {repo} scratch/head-clone-73", elsewhere) is None


def test_clone_remedy_route_is_not_refused_by_its_own_guard(repo, tmp_path, monkeypatch):
    from coordinator_core.bash_guards import _write_bump_applicability

    temp_root = tmp_path / "tmproot"
    scratchpad = temp_root / "claude" / "proj" / "sess-fates" / "scratchpad"
    scratchpad.mkdir(parents=True)
    monkeypatch.setattr(_write_bump_applicability, "_all_temp_roots", lambda: [temp_root])
    nowhere = tmp_path / "norepo"
    nowhere.mkdir()

    refused = _reason(_sub_check(f"git clone {repo} {tmp_path / 'elsewhere' / 'c'}", nowhere))
    assert "session scratchpad" in refused

    named_route = f"git clone {repo} {scratchpad / 'head-clone-73'}"
    assert _sub_check(named_route, nowhere) is None


def test_clone_into_an_arbitrary_dir_still_denies(repo, tmp_path):
    assert "scratch" in _reason(_sub_check(f"git clone {repo} {tmp_path / 'elsewhere' / 'c'}", repo))


# --- bump_foreign_repo_write: a ff-only mirror catch-up passes, a non-ff write still routes -------

SID = "sess-fates-bump"


def _bump_armed(monkeypatch, repos):  # noqa: F811
    from coordinator_core import machine_resolver
    from coordinator_core.ops.fleet import _memo_resolver

    _set_anchor(monkeypatch, repos, SID)
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_OUT_OF_REPO_WRITE_BLOCK", "on")
    machine_profile.reset_cache()
    monkeypatch.setattr(bump, "target_is_publish_destination", lambda *_a, **_k: True)
    monkeypatch.setattr(bump, "publish_destination_owner", lambda *_a, **_k: "owner-em")
    monkeypatch.setattr(_memo_resolver, "read_registry_repos", lambda: {})
    monkeypatch.setattr(machine_resolver, "em_id_for_root", lambda _r, _p: "someone-else-em")


def _bump(repos, cmd):  # noqa: F811
    return bump.check_bump_foreign_repo_write(cmd, SID, str(repos["anchor"]), {})


@pytest.mark.parametrize(
    "verb",
    ["pull --ff-only origin candidate", "pull --ff-only", "fetch origin candidate", "fetch --all"],
)
def test_ff_only_mirror_catch_up_is_not_routed(repos, monkeypatch, verb):  # noqa: F811
    _bump_armed(monkeypatch, repos)
    assert _bump(repos, f"git -C {_posix(repos['foreign'])} {verb}") is None


@pytest.mark.parametrize(
    "verb",
    [
        "pull origin candidate",
        "pull --ff-only /elsewhere/other.git candidate",
        "push origin candidate",
        "merge origin/candidate",
        "commit -m x",
    ],
)
def test_non_ff_mirror_write_still_routes(repos, monkeypatch, verb):  # noqa: F811
    _bump_armed(monkeypatch, repos)
    assert _bump(repos, f"git -C {_posix(repos['foreign'])} {verb}") is not None


def test_ff_only_pull_of_a_non_mirror_peer_still_routes(repos, monkeypatch):  # noqa: F811
    _bump_armed(monkeypatch, repos)
    monkeypatch.setattr(bump, "target_is_publish_destination", lambda *_a, **_k: False)
    assert _bump(repos, f"git -C {_posix(repos['foreign'])} pull --ff-only origin candidate") is not None


def test_bump_guard_never_denies_at_any_level():
    assert deny_registry.guard_deny_class("bump-foreign-repo-write") is None
    envelope = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}
    assert not _denies(machine_profile.apply_guard_level("bump-foreign-repo-write", envelope))


# --- remote delete, variable-held pytest paths: shapes, not irreversible acts ----------------------


def test_xargs_remote_delete_is_guide_only(repo):
    """A remote delete is irreversible but the engine cannot see the bucket path through `xargs`:
    the sentinel guard stays unregistered, so it logs and never denies."""
    assert deny_registry.guard_deny_class("block-approval-sentinel-creation") is None
    cmd = "cat gone.txt | xargs gcloud storage rm"
    record = block_approval_sentinel_creation.check(_payload(cmd, repo))
    resolved = machine_profile.apply_guard_level(
        "block-approval-sentinel-creation", envelope_of(record)
    )
    assert not _denies(resolved)


@pytest.mark.parametrize("cmd", ["python -m pytest $FILES", "pytest $F -q", "python -m pytest $(cat list.txt)"])
def test_variable_held_pytest_path_list_still_blocks(repo, cmd):
    assert deny_registry.guard_deny_class("check-test-suite-invocation") == "box-degrading"
    record = check_test_suite_invocation.check(_payload(cmd, repo))
    resolved = machine_profile.apply_guard_level("check-test-suite-invocation", envelope_of(record))
    assert _denies(resolved)


# --- destructive-git-orphan: an index-only unstage is not a subshell-resolved ref -----------------


def test_index_only_unstage_from_a_list_file_is_not_denied(repo):
    (repo / "list.txt").write_text("a.txt\n")
    cmd = "git reset -q HEAD -- $(cat list.txt)"
    assert not _denies(dispatch.evaluate_payload_json(json.dumps(_payload(cmd, repo))))


def test_reset_to_a_subshell_resolved_ref_still_denies(repo):
    cmd = "git reset -q $(git rev-parse HEAD~1)"
    assert _denies(dispatch.evaluate_payload_json(json.dumps(_payload(cmd, repo))))


def test_reset_with_subshell_ref_before_the_pathspec_still_denies(repo):
    cmd = "git reset -q $(git rev-parse HEAD~1) -- a.txt"
    assert _denies(dispatch.evaluate_payload_json(json.dumps(_payload(cmd, repo))))
