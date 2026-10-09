"""A cloud session's add/commit/push chain on its own `claude/*` branch passes the real
PreToolUse entry (`hooks.preuse_bash_dispatch`) by both doors' payload shapes."""
from __future__ import annotations

import pytest

from coordinator_core.hooks import preuse_bash_dispatch
from coordinator_core.warm.hook_http import FORWARDED_ENV_NAMES, payload_from_event

_CHAIN = "git add notes.md && git commit -m wip && git push -u origin claude/fix-abc123"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/claude/fix-abc123\n", encoding="utf-8")
    return tmp_path


def _event(repo, env=None):
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": _CHAIN},
        "cwd": str(repo),
        "session_id": "s-cloud",
    }
    if env is not None:
        event["env"] = env
    return event


def _topic_denied(out):
    hso = (out or {}).get("hookSpecificOutput") or {}
    return hso.get("permissionDecision") == "deny" and "topic branch" in hso.get("permissionDecisionReason", "")


def test_hook_run_door_forwards_the_cloud_marker(repo, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    payload = payload_from_event(_event(repo, {"CLAUDE_CODE_REMOTE": "true"}), FORWARDED_ENV_NAMES)
    assert not _topic_denied(preuse_bash_dispatch._handler({"payload": payload}))


def test_http_door_reads_the_cloud_host(repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    payload = payload_from_event(_event(repo))
    assert payload["env"] == {}
    assert not _topic_denied(preuse_bash_dispatch._handler({"payload": payload}))


def test_a_desk_still_denies_the_claude_branch(repo, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    payload = payload_from_event(_event(repo, {}), FORWARDED_ENV_NAMES)
    assert _topic_denied(preuse_bash_dispatch._handler({"payload": payload}))
