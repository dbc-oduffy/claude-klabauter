"""A subagent may stage under a scratch GIT_INDEX_FILE and `git apply --cached`; the shared index and worktree writes stay guarded."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.bash_guards import block_subagent_destructive_action as guard

_CACHEINFO = "git update-index --add --cacheinfo 100644,abc123,a/b.py"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _run(command, cwd):
    return guard.check(
        {
            "tool_name": "PowerShell" if command.startswith("$env:") else "Bash",
            "tool_input": {"command": command},
            "session_id": "sess-tmpidx",
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:executor",
            "cwd": str(cwd),
        }
    )


@pytest.mark.parametrize(
    "command",
    [
        "GIT_INDEX_FILE=/tmp/scr/idx git apply --cached --recount p.patch",
        "SCR=/tmp/scr; GIT_INDEX_FILE=$SCR/idx git apply --cached --recount p.patch",
        'SCR=/tmp/scr; GIT_INDEX_FILE="$SCR/idx" git apply --cached --recount p.patch',
        "SCR=/tmp/scr; export GIT_INDEX_FILE=${SCR}/idx; git apply --cached --recount p.patch",
        "SCR=/tmp/scr; GIT_INDEX_FILE=$SCR/idx " + _CACHEINFO,
        'SCR=/tmp/scr; GIT_INDEX_FILE="$SCR/idx" ' + _CACHEINFO,
        '$env:GIT_INDEX_FILE="$scr\\idx"; git apply --cached --recount p.patch',
        "$env:GIT_INDEX_FILE = 'scr/idx'; " + _CACHEINFO,
        "git apply --cached p.patch",
        "git apply --cached --recount p.patch",
    ],
)
def test_scratch_index_staging_is_admitted(repo, command):
    assert _run(command, repo) is None


@pytest.mark.parametrize(
    "command",
    [
        "GIT_INDEX_FILE=.git/index " + _CACHEINFO,
        "GIT_INDEX_FILE= " + _CACHEINFO,
        "GIT_INDEX_FILE=$X " + _CACHEINFO,
        "GIT_INDEX_FILE=$GITDIR/index " + _CACHEINFO,
        "$env:GIT_INDEX_FILE='.git/index'; " + _CACHEINFO,
        _CACHEINFO,
        "git apply p.patch",
        "GIT_INDEX_FILE=/tmp/scr/idx git apply --index p.patch",
        "git apply --cached --3way p.patch",
    ],
)
def test_shared_index_or_worktree_write_stays_denied(repo, command):
    assert _run(command, repo) is not None
