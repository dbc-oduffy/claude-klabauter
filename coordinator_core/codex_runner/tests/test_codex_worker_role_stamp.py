"""An engine-launched codex worker is never classified as the EM by its own SessionStart hooks."""

from __future__ import annotations

import pytest

from coordinator_core.codex_runner import codex_spawn_env as cse
from coordinator_core.session.agent_role import classify, observed_cli_launch

SID = "00000000-0000-4000-8000-000000000001"
BASE = {"hook_event_name": "SessionStart", "session_id": SID, "source": "startup"}
EXEC = ["codex", "exec", "-"]


@pytest.fixture(autouse=True)
def _no_pwsh_check(monkeypatch):
    monkeypatch.setattr(cse, "check_pwsh_launchable", lambda env: None)


@pytest.mark.parametrize("before,after", [(None, "worker"), ("", "worker"), ("em", "worker"), ("reviewer", "reviewer")])
def test_a_rooted_codex_child_is_stamped_worker_unless_it_carries_a_non_em_role(tmp_path, before, after):
    env = {} if before is None else {"COORDINATOR_AGENT_ROLE": before}
    cse.shape_codex_child(env, writable_root=tmp_path / "run", environ={})
    assert env["COORDINATOR_AGENT_ROLE"] == after


def test_an_unrooted_shape_leaves_the_role_alone(tmp_path):
    env: dict[str, str] = {}
    cse.shape_codex_child(env, writable_root=None, environ={})
    assert "COORDINATOR_AGENT_ROLE" not in env


def test_the_stamped_child_classifies_as_a_worker_not_the_em(tmp_path):
    env = {"CODEX_THREAD_ID": SID}
    cse.shape_codex_child(env, writable_root=tmp_path / "run", environ={})
    assert classify(BASE, env, **observed_cli_launch(EXEC)) == "worker"


def test_a_bare_codex_exec_from_a_claude_agent_is_a_worker():
    # The family-stage-runner's Bash `codex exec` inherits CLAUDECODE and nothing else.
    assert classify(BASE, {"CODEX_THREAD_ID": SID, "CLAUDECODE": "1"}, **observed_cli_launch(EXEC)) == "worker"
    assert classify(BASE, {"CODEX_THREAD_ID": SID}, **observed_cli_launch(EXEC)) == "em"
