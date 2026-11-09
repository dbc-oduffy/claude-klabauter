"""A subagent may `git update-index` / `git read-tree` against a private GIT_INDEX_FILE; the shared index stays guarded."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.bash_guards import block_subagent_destructive_action as guard


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _run(command, cwd):
    return guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "sess-pidx",
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:executor",
            "cwd": str(cwd),
        }
    )


@pytest.mark.parametrize(
    "command",
    [
        "GIT_INDEX_FILE=.git/idx-X git read-tree HEAD && "
        "GIT_INDEX_FILE=.git/idx-X git update-index --add --cacheinfo 100644,abc123,a/b.py",
        "export GIT_INDEX_FILE=.git/idx-X; git read-tree HEAD; "
        "git update-index --add --cacheinfo 100644,abc123,a/b.py",
        "export GIT_INDEX_FILE=$PWD/.git/idx-X && git update-index --add --cacheinfo 100644,abc123,a/b.py",
    ],
)
def test_private_index_hand_land_is_allowed(repo, command):
    assert _run(command, repo) is None


@pytest.mark.parametrize(
    "command",
    [
        "git update-index --add --cacheinfo 100644,abc123,a/b.py",
        "GIT_INDEX_FILE=.git/index git update-index --add --cacheinfo 100644,abc123,a/b.py",
        "git update-index --add --cacheinfo 100644,abc123,a/b.py; GIT_INDEX_FILE=.git/idx-X git read-tree HEAD",
        "GIT_INDEX_FILE=.git/idx-X git read-tree -u HEAD",
    ],
)
def test_shared_index_or_worktree_write_stays_denied(repo, command):
    assert _run(command, repo) is not None
