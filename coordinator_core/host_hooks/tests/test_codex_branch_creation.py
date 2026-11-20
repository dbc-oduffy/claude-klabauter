"""Codex sessions are refused non-day-branch creation (local and pushed) at the host shim, warm engine or not."""
from __future__ import annotations

import pytest

from coordinator_core.host_hooks import shim

DAY = "example-operator/machine-a/2026-11-20"


@pytest.fixture(autouse=True)
def no_engine(monkeypatch):
    # Engine dial misses: the degrade path that used to leave a Codex session with no signal.
    monkeypatch.setattr(shim, "_budgeted", lambda request, budget: (None, True))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git" / "refs" / "remotes" / "origin" / "example-operator" / "machine-a").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/%s\n" % DAY)
    (root / ".git" / "config").write_text("[core]\n")
    (root / ".git" / "refs" / "remotes" / "origin" / "main").write_text("x\n")
    (root / ".git" / "refs" / "remotes" / "origin" / "example-operator" / "machine-a" / "2026-11-20").write_text("x\n")
    (root / ".coordinator-local").mkdir()
    return root


def _run(cwd, command, host="codex"):
    return shim.decide(host, {"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "exec_command",
                              "tool_input": {"command": command}, "cwd": str(cwd)})


def _denied(out) -> bool:
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


@pytest.mark.parametrize("command", [
    "git checkout -b example-operator/machine-a/codex-grant-alias",
    "git switch -c topic",
    "git -C . branch topic",
    "cd x && git checkout -B example-operator/machine-a/windows-verify-xs",
    "git push -u origin topic-y",
    "git push origin HEAD:refs/heads/new-topic",
    "git push origin example-operator/machine-a/topic:example-operator/machine-a/topic",
])
def test_codex_creation_is_denied(repo, command):
    assert _denied(_run(repo, command))


@pytest.mark.parametrize("command", [
    "git checkout -b %s" % "example-operator/machine-a/2026-11-21",
    "git checkout -b release/1.0",
    "git switch main",
    "git branch -d old",
    "git branch --list",
    "git push",
    "git push origin %s" % DAY,
    "git push origin HEAD:refs/heads/main",
    "git status",
])
def test_codex_ordinary_work_passes(repo, command):
    assert not _denied(_run(repo, command))


def test_pushing_an_existing_tag_by_bare_name_passes(repo):
    (repo / ".git" / "refs" / "tags").mkdir(parents=True)
    (repo / ".git" / "refs" / "tags" / "v1.0").write_text("x\n")
    assert not _denied(_run(repo, "git push origin v1.0"))
    assert _denied(_run(repo, "git push origin v2.0"))


def test_foreign_repo_is_never_judged(repo):
    (repo / ".coordinator-local").rmdir()
    assert not _denied(_run(repo, "git checkout -b topic"))


def test_claude_hosts_are_untouched(repo):
    from coordinator_core.host_hooks import branch_creation  # noqa: F401
    out = _run(repo, "git checkout -b topic", host="grok")
    assert not _denied(out)


@pytest.mark.parametrize("key", ["command", "cmd"])
def test_every_codex_command_shape_reaches_the_shared_chain(repo, monkeypatch, key):
    """Live Codex denied through the shared chain with `command` (rollout 01a1f58c, guard-heavy-command-admission);
    the exec_command schema names `cmd`. Both must reach the full chain (worktree-creation deny), not just the branch check."""
    out = shim.decide("codex", {"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "exec_command",
                                "tool_input": {key: "git worktree add ../wt-x HEAD"}, "cwd": str(repo)})
    assert _denied(out)
