"""Tests for coordinator_core.bash_guards.block_topic_branch.

Pure parsing: a fake `.git` dir under tmp_path supplies HEAD and config, and
subprocess spawning is made fatal to prove the path spawns nothing.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import block_topic_branch as guard

KEY = "COORDINATOR_OVERRIDE_TOPIC_BRANCH"
DAY = "work/machine-a/2026-10-04"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(f"ref: refs/heads/{DAY}\n", encoding="utf-8")
    monkeypatch.setattr(guard, "_machine", lambda: "machine-a")

    def _no_spawn(*a, **k):
        raise AssertionError("guard spawned a process")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    return tmp_path


def _check(repo, command, env=None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    payload["env"] = env if env is not None else {}
    return guard.check(payload)


def _denied(out):
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "git checkout -b fix/foo",
        "git checkout -B fix/foo",
        "git switch -c fix/foo",
        "git switch -C fix/foo",
        "git switch --create fix/foo",
        "git branch fix/foo",
        "git branch fix/foo main",
        "git push origin fix/foo",
        "git push -u origin fix/foo",
        "git push origin HEAD:fix/foo",
        "git push origin HEAD:refs/heads/fix/foo",
        "git push origin +main:fix/foo",
        "git -C . checkout -b fix/foo",
        "git -C sub -c core.x=1 push origin fix/foo",
        "cd x && git checkout -b fix/foo",
        "git checkout -b work/otherbox/2026-10-05",
        "git push origin work/machine-a/2026-10-05-closeout",
        "git push -q origin fix/foo 2>&1",
        "git push origin fix/foo > out.txt",
        "git push origin fix/foo 2>/dev/null | tail -1",
    ],
)
def test_denies(repo, cmd):
    out = _check(repo, cmd)
    assert _denied(out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert KEY in reason and "Group EM" in reason and "day branch" in reason


@pytest.mark.parametrize(
    "cmd",
    [
        "git push origin main",
        "git push origin HEAD:main",
        "git push",
        "git push origin",
        "git push origin HEAD",
        f"git push origin {DAY}",
        "git push origin work/machine-a/2026-10-05",
        "git checkout -b work/machine-a/2026-10-05",
        "git switch -c work/machine-a/2026-10-05to07",
        "git branch work/machine-a/2026-10-05",
        "git push origin --delete fix/foo",
        "git push origin :fix/foo",
        "git push origin --tags",
        "git push origin refs/tags/v1.2.3",
        "git branch",
        "git branch -a",
        "git branch -v",
        "git branch --list",
        "git branch --show-current",
        "git branch -d fix/foo",
        "git branch -D fix/foo",
        "git branch -m old new",
        "git checkout fix/foo",
        "git switch main",
        "git checkout -b \"$BRANCH\"",
        "git status",
        "git push -q origin work/machine-a/2026-10-05 2>&1",
        "git push origin work/machine-a/2026-10-05 > out.txt 2>&1",
        "git push origin work/machine-a/2026-10-05 2> err.txt",
        "git push origin work/machine-a/2026-10-05 &>log",
        "git push origin main 2>/dev/null | tail -1",
        "git push origin main && echo done",
    ],
)
def test_allows(repo, cmd):
    assert _check(repo, cmd) is None


def test_push_head_denied_when_head_is_a_topic_branch(repo):
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/fix/foo\n", encoding="utf-8")
    assert _denied(_check(repo, "git push origin HEAD"))


def test_configured_day_branch_is_allowed(repo):
    (repo / ".git" / "config").write_text(
        "[coordinator]\n\tdayBranch = harness/cloud-1\n", encoding="utf-8"
    )
    assert _check(repo, "git push origin harness/cloud-1") is None
    assert _denied(_check(repo, "git push origin harness/other"))


def test_inline_override_with_reason_allows(repo):
    assert _check(repo, f"{KEY}=groupem-approved git checkout -b fix/foo") is None
    assert _check(repo, f'{KEY}="group em ok" git push origin fix/foo') is None


def test_inline_override_applies_to_its_own_segment_only(repo):
    out = _check(repo, f"{KEY}=why git checkout -b a/b && git checkout -b c/d")
    assert _denied(out)
    assert "c/d" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("prefix", [f"{KEY}=", f'{KEY}=""', f"{KEY}=   "])
def test_empty_override_still_denies(repo, prefix):
    assert _denied(_check(repo, f"{prefix} git checkout -b fix/foo"))


def test_env_var_alone_never_overrides(repo, monkeypatch):
    assert _denied(_check(repo, "git checkout -b fix/foo", env={KEY: "why"}))
    monkeypatch.setenv(KEY, "why")
    assert _denied(_check(repo, "git checkout -b fix/foo", env=None))


@pytest.mark.parametrize("cmd", ["ls -la", "python -m pytest x.py", "echo hi && cat f"])
def test_non_git_commands_spawn_nothing_and_skip_machine_lookup(repo, monkeypatch, cmd):
    def _boom():
        raise AssertionError("machine lookup ran for a non-git command")

    monkeypatch.setattr(guard, "_machine", _boom)
    assert _check(repo, cmd) is None


def test_main_and_nonbranch_git_skip_machine_lookup(repo, monkeypatch):
    def _boom():
        raise AssertionError("machine lookup ran")

    monkeypatch.setattr(guard, "_machine", _boom)
    assert _check(repo, "git push origin main") is None
    assert _check(repo, "git status") is None


def test_non_bash_tool_allows(repo):
    assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": "x"}}) is None
