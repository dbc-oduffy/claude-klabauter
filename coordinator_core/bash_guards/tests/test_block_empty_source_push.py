"""block_empty_source_push: empty or unresolved-variable push sources are denied."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_empty_source_push as guard
from coordinator_core.guard_messages.testing import envelope_of

SHA = "0123456789abcdef0123456789abcdef01234567"

DENIED = [
    "git push origin $C:refs/heads/x",
    "git push origin :refs/heads/x",
    "git push --delete origin x",
    "git push origin -d x",
    "git push -df origin x",
    'git push origin "${C}":main',
    "git push origin +$C:refs/heads/x",
    "git push origin $(git rev-parse FETCH_HEAD):refs/heads/x",
    "cd /tmp && git push origin $C:refs/heads/x",
]

ALLOWED = [
    f"git push origin {SHA}:refs/heads/x",
    "git push origin main",
    "git push",
    "git push --dry-run origin :x",
    "git push -n origin $C:x",
    "git push origin HEAD:refs/heads/x",
    "git status",
]


def _payload(cmd, env=None, tool="Bash"):
    p = {"tool_name": tool, "tool_input": {"command": cmd}, "cwd": "/tmp"}
    if env is not None:
        p["env"] = env
    return p


@pytest.mark.parametrize("cmd", DENIED)
def test_denied(cmd):
    assert guard.check(_payload(cmd)) is not None


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(cmd):
    assert guard.check(_payload(cmd)) is None


@pytest.mark.parametrize("cmd", DENIED)
def test_override_env_allows(cmd):
    assert guard.check(_payload(cmd, env={guard.OVERRIDE_ENV: "1"})) is None


def test_override_value_other_than_one_still_denies():
    assert guard.check(_payload(DENIED[0], env={guard.OVERRIDE_ENV: "0"})) is not None


def test_non_command_tool_ignored():
    assert guard.check(_payload(DENIED[0], tool="Read")) is None


def test_deny_message_names_anchor_not_override_key():
    text = str(envelope_of(guard.check(_payload(DENIED[0]))))
    assert guard.ANCHOR in text
    assert guard.OVERRIDE_ENV not in text


def test_interface():
    assert guard.CLASS == "hard-deny"
    assert guard.OVERRIDE_ENV == "COORDINATOR_OVERRIDE_PUSH_DELETE"
