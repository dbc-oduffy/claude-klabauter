"""The EM's clone-per-run grid command passes the guard chain; a subagent running the harness script stays denied."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from coordinator_core.bash_guards import block_approval_sentinel_creation as sentinel_guard
from coordinator_core.bash_guards import block_subagent_commit as commit_guard
from coordinator_core.bash_guards import guard_post_executor_verification as verify_guard
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.hooks import nudge_em_code_dispatch as nd

SID = "sess-clone-route-1"
REPO_ROOT = Path(__file__).resolve().parents[3]

GRID_COMMAND = (
    "python -m coordinator_core.benchmarks.measure --clone-per-run --k 1 --json -- "
    "python -I '{clone}/coordinator_core/benchmarks/commit_leg_profile.py' '{clone}' "
    "phased --paths 30 --sign config"
)
HARNESS_COMMAND = "python -I coordinator_core/benchmarks/commit_leg_profile.py /x phased"


@pytest.fixture
def armed_repo(monkeypatch, tmp_path):
    """A tmp repo whose session has a dispatched executor behind it, as test_post_executor_verification builds one."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(nd, "_is_bootstrap_or_out_of_repo", lambda p: False)
    monkeypatch.setattr(nd, "_session_sizing_state", lambda payload, sid: (True, False))
    monkeypatch.setattr(nd, "resolve_mode", lambda k, sid: False)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    verify_guard._PKG_CACHE.clear()
    session_dir = repo / ".git" / "coordinator-sessions" / SID
    session_dir.mkdir(parents=True)
    (session_dir / "dispatched-agents.txt").write_text("abcdef0123456789\tsonnet\tcoordinator:executor\t1\n")
    return repo


def _bash(cwd, command, **extra) -> dict:
    payload = {
        "tool_name": "Bash",
        "session_id": SID,
        "cwd": str(cwd),
        "tool_input": {"command": command},
    }
    payload.update(extra)
    return payload


def _counter(repo):
    return next(repo.parent.rglob(verify_guard.COUNTER_NAME), None) or next(
        (repo / ".git").rglob(verify_guard.COUNTER_NAME), None
    )


def _denied(out) -> bool:
    out = envelope_of(out, "em")
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_post_executor_verification_neither_denies_nor_counts_the_grid_command(armed_repo) -> None:
    assert verify_guard.check(_bash(armed_repo, "python -m pytest")) is None
    counter = _counter(armed_repo)
    assert counter is not None and counter.read_text() == "1"
    for _ in range(3):
        assert verify_guard.check(_bash(armed_repo, GRID_COMMAND)) is None
    assert _counter(armed_repo).read_text() == "1"


def test_approval_sentinel_guard_allows_the_grid_command(armed_repo) -> None:
    assert sentinel_guard.check(_bash(armed_repo, GRID_COMMAND)) is None


def test_subagent_commit_guard_allows_the_grid_command_from_the_em(armed_repo) -> None:
    payload = _bash(armed_repo, GRID_COMMAND)
    assert "agent_id" not in payload
    assert commit_guard.check(payload) is None


def test_subagent_running_the_harness_script_in_the_repo_is_still_denied() -> None:
    script = REPO_ROOT / "coordinator_core" / "benchmarks" / "commit_leg_profile.py"
    assert "from coordinator_core.git.commit import" in script.read_text(encoding="utf-8")
    out = envelope_of(commit_guard.check(_bash(REPO_ROOT, HARNESS_COMMAND, agent_id="deadbeef0123")), "em")
    assert _denied(out)
    assert "script file" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_em_running_the_harness_script_directly_is_not_the_denied_shape() -> None:
    assert commit_guard.check(_bash(REPO_ROOT, HARNESS_COMMAND)) is None
