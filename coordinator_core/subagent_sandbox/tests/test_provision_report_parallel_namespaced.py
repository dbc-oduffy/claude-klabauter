"""Parallel unnamed namespaced dispatches provision even when the spawning git-root probe fails."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from coordinator_core.subagent_sandbox import provision_report
from coordinator_core.subagent_sandbox.provision_report import _provision

SESSION = "69d9a3a5-fe14-49b5-809c-39053c7d241f"
DISPATCHES = (
    ("a20b235106f5a4307", "coordinator:security-audit-worker"),
    ("a1282c99334b4b9f4", "coordinator:staff-eng"),
)


@pytest.fixture
def repo_and_policy(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump({"report_sidecar": [t for _, t in DISPATCHES]}), encoding="utf-8"
    )
    return repo, policy


def _payload(agent_id: str, agent_type: str) -> dict:
    return {"agent_id": agent_id, "agent_type": agent_type, "session_id": SESSION}


def _run_parallel(repo: Path, policy: Path) -> list:
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_provision, _payload(a, t), str(policy), str(repo)) for a, t in DISPATCHES
        ]
        return [f.result() for f in futures]


def _assert_each_provisioned(repo: Path, results: list) -> None:
    assert all(results), results
    assert len(set(results)) == 2
    for (agent_id, agent_type), rel in zip(DISPATCHES, results):
        assert agent_id in rel
        assert f"agent_type: {agent_type}" in (repo / rel).read_text(encoding="utf-8")


def test_parallel_unnamed_namespaced_dispatches_each_provision(repo_and_policy):
    repo, policy = repo_and_policy
    _assert_each_provisioned(repo, _run_parallel(repo, policy))


def test_provision_survives_timed_out_git_root_spawn(repo_and_policy, monkeypatch):
    """`resolve_git_root` returns None on its 2s timeout; the walk must carry the call."""
    repo, policy = repo_and_policy
    monkeypatch.setattr(provision_report, "resolve_git_root", lambda cwd=None: None)
    _assert_each_provisioned(repo, _run_parallel(repo, policy))


def test_provision_still_refuses_a_cwd_with_no_repo(tmp_path, repo_and_policy, monkeypatch):
    _repo, policy = repo_and_policy
    monkeypatch.setattr(provision_report, "resolve_git_root", lambda cwd=None: None)
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    assert _provision(_payload(*DISPATCHES[0]), str(policy), str(bare)) is None
