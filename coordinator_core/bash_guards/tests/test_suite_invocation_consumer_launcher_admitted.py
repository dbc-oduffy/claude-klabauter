"""In a repo that is not claude-klabauter, a workflow leg's scoped launcher command and its file-scoped fallback
reach no suite-guard deny; broad shapes stay denied."""

from __future__ import annotations

import types

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as suite
from coordinator_core.bash_guards import guard_heavy_command_admission as heavy
from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.session import touch_record

TARGET = "tests/install/test_probe.py"
_RUN_TRANSCRIPT = "projects/p/sess/subagents/workflows/wf_abc/agent-a1.jsonl"
TEST_RUNNER = "coordinator:test-runner"
EXECUTOR = "coordinator:executor"


@pytest.fixture(autouse=True)
def healthy_box(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(heavy, "_read_config", lambda: {"heavy_admission.free_ram_floor_mb": "4096"})
    monkeypatch.setattr(
        heavy, "_read_available", lambda: types.SimpleNamespace(trusted=True, avail_mb=32000, source="t")
    )
    monkeypatch.setattr(heavy, "_read_box_hold", lambda: None)
    monkeypatch.setattr(heavy, "_append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        touch_record, "project_live_claims", lambda *a, **k: touch_record.TouchProjection(claims={}, degraded=False)
    )


def _payload(repo, command, agent_type):
    return {
        "tool_name": "Bash",
        "session_id": "sess-A",
        "agent_id": "a1",
        "agent_type": agent_type,
        "cwd": str(repo),
        "transcript_path": _RUN_TRANSCRIPT,
        "tool_input": {"command": command},
    }


def _denied(result) -> bool:
    return result is not None and getattr(result, "verdict_kind", None) == "deny"


@pytest.mark.parametrize("agent_type", [TEST_RUNNER, EXECUTOR])
def test_the_emitted_launcher_command_for_a_consumer_repo_is_not_denied(tmp_path, agent_type):
    command = emit._launcher_command([TARGET], TARGET, tmp_path)
    assert not _denied(suite.check(_payload(tmp_path, command, agent_type)))


@pytest.mark.parametrize("agent_type", [TEST_RUNNER, EXECUTOR])
def test_the_installed_launcher_form_is_not_denied(tmp_path, agent_type):
    command = f"scoped-pytest --allow {TARGET} -- {TARGET}"
    assert not _denied(suite.check(_payload(tmp_path, command, agent_type)))


def test_the_test_runner_file_scoped_fallback_is_not_denied(tmp_path):
    assert not _denied(suite.check(_payload(tmp_path, f"python -m pytest {TARGET}", TEST_RUNNER)))


def test_the_executor_row_gates_file_scoped_fallback_is_not_denied(tmp_path):
    """The executor row gate's file-scoped fallback is admitted; broad shapes stay denied below."""
    assert not _denied(suite.check(_payload(tmp_path, f"python -m pytest {TARGET}", EXECUTOR)))


@pytest.mark.parametrize("agent_type", [TEST_RUNNER, EXECUTOR])
@pytest.mark.parametrize(
    "command",
    ["python -m pytest", "python -m pytest tests/", f"python -m pytest {TARGET} -n 4"],
    ids=["bare", "directory", "workers"],
)
def test_broad_shapes_stay_denied_in_a_consumer_repo(tmp_path, command, agent_type):
    assert _denied(suite.check(_payload(tmp_path, command, agent_type)))
