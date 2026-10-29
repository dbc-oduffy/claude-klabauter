"""Every guard denial of a git add or git commit names the one commit route (`COMMIT_ROUTE`) and
none offers `git add --pathspec-from-file` or `git -C add|commit` as the way forward.

Drives the repro commands of state/bug-backlog/2026-10-27-commit-blocking-guard-denials-chain-into-a4d6cc889cc8.yaml
through blanket-git-add, the post-fire product-write deny and block-subagent-destructive-action,
and reads block_subagent_commit's leg messages.
"""

from __future__ import annotations

import re
import tempfile

import pytest

from coordinator_core.bash_guards import _dc_git_probe, _dc_git_shared
from coordinator_core.bash_guards import block_subagent_commit as commit_guard
from coordinator_core.bash_guards import block_subagent_destructive_action as destructive
from coordinator_core.bash_guards import dispatch_checks as blanket
from coordinator_core.bash_guards._commit_route import COMMIT_ROUTE
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.hooks import _em_post_fire as post_fire

SESSION = "sess-c4"

_WRONG_ROUTE = re.compile(
    r"--pathspec-from-file|(?:instead|route|use)[^\n]*git -C \S+ (?:add|commit)", re.IGNORECASE
)


def _reason(envelope) -> str:
    return envelope["hookSpecificOutput"]["permissionDecisionReason"]


def _assert_names_route_only(text: str) -> None:
    offered = "\n".join(
        ln for ln in text.splitlines() if not ln.strip().startswith(("Command:", "Denied:", "Matched:"))
    )
    assert COMMIT_ROUTE in text, text
    assert not _WRONG_ROUTE.search(offered), text


def _blanket_deny(monkeypatch, tmp_path, cmd) -> str:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)

    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return 0, str(root) + "\n"
        if args[:3] == ["diff", "--cached", "--name-only"]:
            return 0, ""
        raise AssertionError("unexpected git invocation: %r" % (args,))

    monkeypatch.setattr(_dc_git_probe, "_run_git", _fake_run_git)
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda r: True)
    monkeypatch.chdir(root)
    result = envelope_of(blanket.check_blanket_git_add(cmd, session_id="sess1", hook_payload={}))
    assert result is not None, cmd
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return _reason(result)


def test_blanket_git_c_add_names_the_route(monkeypatch, tmp_path):
    text = _blanket_deny(monkeypatch, tmp_path, "git -C /some/dir add -A")
    _assert_names_route_only(text)


def test_blanket_commit_side_names_the_route(monkeypatch, tmp_path):
    text = _blanket_deny(monkeypatch, tmp_path, "git commit -m x -- .")
    _assert_names_route_only(text)


@pytest.mark.parametrize("paths", [(), ("state/records/a.yaml",), ("CLAUDE.md",)])
def test_post_fire_deny_names_the_route(paths):
    envelope = post_fire.deny_envelope(paths)
    assert envelope is not None
    _assert_names_route_only(_reason(envelope))


@pytest.fixture
def world(tmp_path, monkeypatch):
    temp = tmp_path / "tmp"
    pad = temp / "claude" / "proj" / SESSION / "scratchpad"
    pad.mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(var, str(temp))
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    return pad, repo


def _destructive(command, cwd):
    return envelope_of(
        destructive.check(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command.replace("\\", "/")},
                "session_id": SESSION,
                "cwd": str(cwd),
                "agent_id": "deadbeef0123",
                "agent_type": "coordinator:git-commit-agent",
            }
        )
    )


@pytest.mark.parametrize("sub", ["add --pathspec-from-file=list.txt", "add -- a.py", "commit -m x -- a.py"])
def test_subagent_git_c_into_the_real_repo_names_the_route(world, sub):
    pad, repo = world
    result = _destructive(f"git -C {repo} {sub}", pad)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    _assert_names_route_only(_reason(result))


def test_subagent_commit_leg_messages_offer_one_route():
    texts = list(commit_guard._GIT_COMMIT_AGENT_LEG_MESSAGES.values())
    texts.append(commit_guard._GIT_COMMIT_AGENT_DENY_REASON)
    for text in texts:
        assert not _WRONG_ROUTE.search(text), text
        assert "git commit -m" not in text, text
    assert COMMIT_ROUTE.split(" (")[0] in commit_guard._GIT_COMMIT_AGENT_DENY_REASON
    for leg in (commit_guard._LEG_NO_PATHSPEC, commit_guard._LEG_UNRESOLVABLE_GIT_ROOT):
        assert COMMIT_ROUTE.split(" (")[0] in commit_guard._GIT_COMMIT_AGENT_LEG_MESSAGES[leg]


def test_blanket_add_offers_only_the_commit_route(monkeypatch, tmp_path):
    text = _blanket_deny(monkeypatch, tmp_path, "git add -A")
    _assert_names_route_only(text)
    assert "git add -- path/to/file" not in text


def test_git_probe_scope_clause_names_the_route():
    assert COMMIT_ROUTE in _dc_git_probe._SAFE_COMMIT_ROUTE_CLAUSE
    assert "coordinator-safe-commit" not in _dc_git_probe._SAFE_COMMIT_ROUTE_CLAUSE


def test_commit_guard_scope_denial_offers_the_route():
    from coordinator_core.bash_guards import _dc_commit

    record = _dc_commit._scope_denial("BLOCKED (strict scope): x", foreign_hunk=False)
    text = repr(record)
    assert COMMIT_ROUTE.split(" (")[0] in text
    assert "coordinator-safe-commit" not in text


def test_approval_sentinel_denial_names_the_route_only():
    from coordinator_core.bash_guards import block_approval_sentinel_creation as sentinel
    from coordinator_core.bash_guards.tests.test_block_approval_sentinel_creation import (
        SENTINEL,
        _enveloped,
        _payload,
    )

    out = _enveloped(sentinel.check)(_payload("touch %s" % SENTINEL))
    _assert_names_route_only(_reason(out))
