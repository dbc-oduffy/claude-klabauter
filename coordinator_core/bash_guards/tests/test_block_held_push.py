"""Tests for coordinator_core.bash_guards.block_held_push.

A fake `.git` dir under tmp_path supplies HEAD and config; subprocess spawning is made fatal
to prove the guard path spawns nothing. The dispatch-entry tests drive the production chain.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core import deny_registry
from coordinator_core.bash_guards import block_held_push as guard
from coordinator_core.guard_messages.testing import envelope_of

DAY = "work/machine-a/2026-10-04"
SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(f"ref: refs/heads/{DAY}\n", encoding="utf-8")

    def _no_spawn(*a, **k):
        raise AssertionError("guard spawned a process")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    return tmp_path


def _hold(repo, text):
    (repo / ".git" / "config").write_text(text, encoding="utf-8")


def _branch_hold(repo, allow=None):
    extra = f"\tcoordinatorPushHoldAllow = {allow}\n" if allow else ""
    _hold(repo, f'[branch "{DAY}"]\n\tcoordinatorPushHold = pr freeze\n{extra}')


def _payload(repo, command, tool="Bash"):
    return {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(repo), "env": {}}


def _check(repo, command, tool="Bash"):
    return envelope_of(guard.check(_payload(repo, command, tool)))


def _denied(out):
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_held_branch_push_refused_for_every_shape(repo, tool):
    _branch_hold(repo)
    for cmd in ("git push", "git push origin HEAD", f"git push origin {DAY}", f"git push origin x:{DAY}"):
        out = _check(repo, cmd, tool)
        assert _denied(out), (tool, cmd)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pr freeze" in reason and DAY in reason
    assert "push-hold clear" in reason


def test_no_override_env_clears_the_hold(repo):
    _branch_hold(repo)
    for prefix in ("COORDINATOR_OVERRIDE_TOPIC_BRANCH=because", "COORDINATOR_OVERRIDE_PUSH_DELETE=1"):
        assert _denied(_check(repo, f"{prefix} git push origin HEAD"))


def test_allow_sha_refspec_admitted_and_other_source_refused(repo):
    _branch_hold(repo, SHA_A)
    assert not _denied(_check(repo, f"git push origin {SHA_A}:refs/heads/{DAY}"))
    assert not _denied(_check(repo, f"git push origin {SHA_A[:9]}:{DAY}"))
    assert _denied(_check(repo, f"git push origin {SHA_B}:refs/heads/{DAY}"))
    assert _denied(_check(repo, "git push origin HEAD"))


def test_unheld_branch_and_non_push_unaffected(repo):
    _hold(repo, '[branch "work/machine-a/other"]\n\tcoordinatorPushHold = n\n')
    assert not _denied(_check(repo, "git push origin HEAD"))
    assert not _denied(_check(repo, "git push"))
    _branch_hold(repo)
    assert not _denied(_check(repo, "git status"))
    assert not _denied(_check(repo, "git push origin fix/other"))


def test_repo_hold_blocks_any_branch(repo):
    _hold(repo, "[coordinator]\n\tpushHold = freeze\n")
    assert _denied(_check(repo, "git push origin main"))
    assert _denied(_check(repo, "git push origin HEAD"))


def test_dash_c_and_cd_resolve_the_hold_repo(repo, tmp_path_factory):
    _branch_hold(repo)
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    assert _denied(envelope_of(guard.check(_payload(elsewhere, f'git -C "{repo}" push origin HEAD'))))
    assert _denied(envelope_of(guard.check(_payload(elsewhere, f'cd "{repo}" && git push origin HEAD'))))


@pytest.mark.parametrize(
    "cmd", [f"git push origin :{DAY}", f"git push --delete origin {DAY}", f"git push origin -d {DAY}"]
)
def test_delete_of_held_branch_refused_even_with_allow(repo, cmd):
    _branch_hold(repo, SHA_A)
    assert _denied(_check(repo, cmd))


def test_all_and_mirror_refused_while_any_hold_exists(repo):
    _branch_hold(repo)
    assert _denied(_check(repo, "git push --all origin"))
    assert _denied(_check(repo, "git push --mirror origin"))


def test_non_command_tool_allows(repo):
    _branch_hold(repo)
    assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": "x"}}) is None


def test_guard_is_hard_deny_and_registered_irreversible():
    assert guard.CLASS == "hard-deny"
    assert deny_registry.GUARD_DENY_CLASS["block-held-push"] == deny_registry.IRREVERSIBLE_EXTERNAL


def _dispatch(repo, command, tool="Bash"):
    from coordinator_core.bash_guards import dispatch

    payload = {
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": "sess-held-push",
        "tool_use_id": "tu-held-push",
        "cwd": str(repo),
    }
    return dispatch.evaluate_payload_json(json.dumps(payload))


@pytest.fixture
def spawning_repo(tmp_path):
    """The same fake repo with spawning allowed: the dispatch chain's other guards probe git."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(f"ref: refs/heads/{DAY}\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_dispatch_entry_denies_a_plain_push_to_a_held_branch(spawning_repo, tool):
    repo = spawning_repo
    _branch_hold(repo)
    for cmd in ("git push", "git push origin HEAD"):
        hso = (_dispatch(repo, cmd, tool) or {}).get("hookSpecificOutput", {})
        assert hso.get("permissionDecision") == "deny", (tool, cmd, hso)
        assert "`block-held-push`" in hso["permissionDecisionReason"]
        assert "pr freeze" in hso["permissionDecisionReason"]


def test_dispatch_entry_admits_the_allow_sha_refspec(spawning_repo):
    repo = spawning_repo
    _branch_hold(repo, SHA_A)
    out = _dispatch(repo, f"git push origin {SHA_A}:refs/heads/{DAY}") or {}
    assert "block-held-push" not in json.dumps(out)
