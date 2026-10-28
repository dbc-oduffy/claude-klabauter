"""Tests for coordinator_core.bash_guards.block_topic_branch.

Pure parsing: a fake `.git` dir under tmp_path supplies HEAD and config, and
subprocess spawning is made fatal to prove the path spawns nothing.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import block_topic_branch as guard
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))

KEY = "COORDINATOR_OVERRIDE_TOPIC_BRANCH"
DAY = "work/machine-a/2026-10-04"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(f"ref: refs/heads/{DAY}\n", encoding="utf-8")

    def _no_spawn(*a, **k):
        raise AssertionError("guard spawned a process")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    return tmp_path


def _check(repo, command, env=None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    payload["env"] = env if env is not None else {}
    return _enveloped(guard.check)(payload)


def _denied(out):
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "git checkout -b fix/foo",
        "git switch -c fix/foo",
        "git branch fix/foo",
        "git push origin fix/foo",
        "git push origin HEAD:fix/foo",
        "git push origin main",
        "git push",
        "git push origin --tags",
        "git status",
        "ls -la",
    ],
)
def test_topic_branch_shapes_emit_nothing(repo, cmd):
    assert _check(repo, cmd) is None


def test_push_head_of_a_topic_branch_emits_nothing(repo):
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/fix/foo\n", encoding="utf-8")
    assert _check(repo, "git push origin HEAD") is None


def _on_main(root, remote=True):
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    if remote:
        (root / ".git" / "config").write_text('[remote "origin"]\n\turl = x\n', encoding="utf-8")


def test_commit_on_remote_backed_main_denied(repo):
    _on_main(repo)
    out = _check(repo, "git commit -m x")
    assert _denied(out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "BLOCKED" not in reason and KEY not in reason


def test_commit_dash_c_judged_on_that_repo(repo, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    (other / ".git").mkdir()
    _on_main(other)
    assert _denied(_check(repo, f"git -C {other} commit -m x"))
    assert _check(other, f"git -C {repo} commit -m x") is None


def test_commit_after_cd_judged_on_that_repo(repo, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    (other / ".git").mkdir()
    _on_main(other)
    assert _denied(_check(repo, f"cd {other} && git commit -m x"))


def test_commit_override_prefix_allows(repo):
    _on_main(repo)
    assert _check(repo, f"{KEY}=merge git commit -m x") is None
    assert _denied(_check(repo, f"{KEY}= git commit -m x"))


def test_wrapped_override_holds_inside_sh_c(repo):
    _on_main(repo)
    assert not _denied(_check(repo, f"{KEY}=why sh -c 'git commit -m x'"))
    assert _denied(_check(repo, "sh -c 'git commit -m x'"))


def test_commit_on_main_inline_override_branch_marker(repo):
    _on_main(repo)
    assert _denied(_check(repo, "git commit -m x"))
    assert _check(repo, "COORDINATOR_OVERRIDE_BRANCH=1 git commit -m x") is None
    assert _denied(_check(repo, "COORDINATOR_OVERRIDE_BRANCH=0 git commit -m x"))
    assert _denied(_check(repo, "git commit -m 'COORDINATOR_OVERRIDE_BRANCH=1'"))


def test_commit_on_work_branch_or_remoteless_main_allows(repo):
    assert _check(repo, "git commit -m x") is None
    _on_main(repo, remote=False)
    assert _check(repo, "git commit -m x") is None


def test_non_bash_tool_allows(repo):
    assert _enveloped(guard.check)({"tool_name": "Edit", "tool_input": {"file_path": "x"}}) is None


SHA_A = "a" * 40
SHA_B = "b" * 40


def _hold(repo, text):
    cfg = repo / ".git" / "config"
    cfg.write_text(text, encoding="utf-8")


def _branch_hold(repo, allow=None):
    extra = f"\tcoordinatorPushHoldAllow = {allow}\n" if allow else ""
    _hold(repo, f'[branch "{DAY}"]\n\tcoordinatorPushHold = pr freeze\n{extra}')


def test_held_branch_push_refused(repo):
    _branch_hold(repo)
    for cmd in ("git push", "git push origin HEAD", f"git push origin {DAY}", f"git push origin x:{DAY}"):
        out = _check(repo, cmd)
        assert _denied(out), cmd
    assert "pr freeze" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "push-hold clear" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_override_env_does_not_clear_hold(repo):
    _branch_hold(repo)
    assert _denied(_check(repo, f"{KEY}=because git push origin HEAD"))


def test_allowed_sha_refspec_admitted(repo):
    _branch_hold(repo, SHA_A)
    assert not _denied(_check(repo, f"git push origin {SHA_A}:refs/heads/{DAY}"))
    assert not _denied(_check(repo, f"git push origin {SHA_A[:9]}:{DAY}"))


def test_other_sha_or_bare_push_refused_with_allow(repo):
    _branch_hold(repo, SHA_A)
    assert _denied(_check(repo, f"git push origin {SHA_B}:refs/heads/{DAY}"))
    assert _denied(_check(repo, "git push origin HEAD"))


def test_unheld_branch_unaffected(repo):
    _hold(repo, '[branch "work/machine-a/other"]\n\tcoordinatorPushHold = n\n')
    assert not _denied(_check(repo, "git push origin HEAD"))
    assert not _denied(_check(repo, "git push"))


def test_repo_hold_blocks_any_branch(repo):
    _hold(repo, "[coordinator]\n\tpushHold = freeze\n")
    assert _denied(_check(repo, "git push origin main"))
    assert _denied(_check(repo, f"git push origin HEAD"))


def test_dash_c_resolves_hold_repo(repo, tmp_path_factory):
    _branch_hold(repo)
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    payload = {"tool_name": "Bash", "tool_input": {"command": f'git -C "{repo}" push origin HEAD'},
               "cwd": str(elsewhere), "env": {}}
    assert _denied(_enveloped(guard.check)(payload))


@pytest.mark.parametrize("cmd", [f"git push origin :{DAY}", f"git push --delete origin {DAY}", f"git push origin -d {DAY}"])
def test_delete_of_held_branch_refused_even_with_allow(repo, cmd):
    _branch_hold(repo, SHA_A)
    assert _denied(_check(repo, cmd))


def test_guard_is_advisory_and_unregistered():
    from coordinator_core import deny_registry

    assert guard.CLASS == "advisory"
    assert "block-topic-branch" not in deny_registry.GUARD_DENY_CLASS


def test_main_commit_verdict_is_clamped_to_an_advisory_at_the_policy_point(repo):
    from coordinator_core.machine_profile import apply_guard_level

    _on_main(repo)
    verdict = _check(repo, "git commit -m x")
    assert _denied(verdict)
    clamped = apply_guard_level("block-topic-branch", verdict)
    assert clamped["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_non_owned_remote_push_deny_stays_registered():
    from coordinator_core import deny_registry

    assert deny_registry.GUARD_DENY_CLASS["block-push-pr-non-owned-remote"] == deny_registry.IRREVERSIBLE_EXTERNAL
