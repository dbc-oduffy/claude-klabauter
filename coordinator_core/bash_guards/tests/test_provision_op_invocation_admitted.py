"""A subagent running the coordinator-invoke repo.provision_local command line is denied by none of
bump-outside-repo-write, bump-foreign-repo-write or block-subagent-destructive-action: the op does
its mkdir and git init in-process, so no write sink or destructive verb is in the command."""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as destructive
from coordinator_core.bash_guards import bump_foreign_repo_write as foreign
from coordinator_core.bash_guards import bump_outside_repo_write as outside
from coordinator_core.bash_guards.tests.test_bump_foreign_repo_write import (
    _clean_bump_env,  # noqa: F401 -- autoused fixture
    _reset_git_root_cache,  # noqa: F401 -- autoused fixture
    _set_anchor,
    repos,  # noqa: F401 -- fixture
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "sess-provision-op"
_PARAMS = {"manifest": "templates/demo-vault-guide.yaml", "assent": "state/sizings/x.yaml#R1"}
_CMDS = [
    "coordinator-invoke repo.provision_local '" + json.dumps(_PARAMS) + "'",
    "coordinator-invoke repo.provision_local --params-file - <<'JSON'\n" + json.dumps(_PARAMS) + "\nJSON",
]


@pytest.mark.parametrize("cmd", _CMDS)
def test_bump_guards_admit_the_invocation(repos, monkeypatch, cmd):
    _set_anchor(monkeypatch, repos, _SID)
    cwd = str(repos["anchor"])
    payload = {"agent_id": "deadbeef0123", "session_id": _SID}
    assert outside.check_bump_outside_repo_write(cmd, _SID, cwd, payload) is None
    assert foreign.check_bump_foreign_repo_write(cmd, _SID, cwd, payload) is None


@pytest.mark.parametrize("cmd", _CMDS)
def test_destructive_action_guard_admits_the_invocation(repos, cmd):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": _SID,
        "cwd": str(repos["anchor"]),
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
    }
    assert destructive.check(payload) is None
