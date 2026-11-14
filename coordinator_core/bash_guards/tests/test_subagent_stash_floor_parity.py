"""Subagent stash deny parity with the DoE fail-open floor's verb set.

FLOOR_VERBS is pinned as a literal from coordinator-content-repo
8b2a1c5acb851187456409e6adec68cab0f18978
coordinator/hooks/scripts/preuse-bash-dispatch.py `_STASH_CREATE_RE`; no
sibling checkout is read.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.guard_messages.testing import envelope_of

pytestmark = pytest.mark.complete

# Source: coordinator-content-repo 8b2a1c5acb coordinator/hooks/scripts/preuse-bash-dispatch.py
# `_STASH_CREATE_RE` lookahead: bare | push|save|create|apply (pop dropped).
FLOOR_VERBS = ("", "push", "save", "create", "apply")


def _payload(command, subagent=True):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
    }
    if subagent:
        p["agent_id"] = "deadbeef0123"
        p["agent_type"] = "coordinator:executor"
    return p


def _verdict(command, subagent=True):
    return envelope_of(guard.check(_payload(command, subagent)))


def _cmd(verb):
    return f"git stash {verb}".strip()


@pytest.mark.parametrize("verb", FLOOR_VERBS)
def test_subagent_denied_anchored(verb):
    assert _verdict(_cmd(verb)) is not None


@pytest.mark.parametrize("verb", FLOOR_VERBS)
def test_subagent_denied_legacy_path(verb):
    assert guard._evaluate_git_segment_legacy(_cmd(verb)) is not None


@pytest.mark.parametrize("verb", FLOOR_VERBS)
def test_em_payload_unchanged(verb):
    assert _verdict(_cmd(verb), subagent=False) is None


def test_create_with_dashdash_still_denied():
    assert _verdict("git stash create -- x") is not None
    assert guard._evaluate_git_segment_legacy("git stash create -- x") is not None


@pytest.mark.parametrize("verb", ("list", "show", "branch foo", "store abc"))
def test_non_floor_verbs_keep_verdict(verb):
    assert _verdict(_cmd(verb)) is None


@pytest.mark.parametrize("verb", FLOOR_VERBS[1:])
def test_quoted_literal_not_denied(verb):
    assert _verdict(f'echo "git stash {verb}"') is None


@pytest.mark.parametrize("verb", FLOOR_VERBS[1:])
def test_heredoc_body_not_denied(verb):
    cmd = f"cat <<'EOF'\ngit stash {verb}\nEOF"
    assert _verdict(cmd) is None


def test_subagent_pop_allowed_anchored_and_legacy():
    assert _verdict("git stash pop") is None
    assert _verdict("git stash pop stash@{1}") is None
    assert guard._evaluate_git_segment_legacy("git stash pop") is None


def test_em_pop_never_denied():
    assert _verdict("git stash pop", subagent=False) is None
