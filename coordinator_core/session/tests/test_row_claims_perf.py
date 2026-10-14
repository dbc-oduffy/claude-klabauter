"""Row claim and release cost process time and spawn count, never wall clock."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.session import claims, liveness

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CLI = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "session-claim-cli.py"
_ROW = "state/bug-backlog/2026-10-14-perf-row.yaml"
_BUDGET_MS = 500
_K = 10


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _env():
    env = dict(os.environ)
    env["COORDINATOR_SESSION_ID"] = "sid-perf"
    return env


@pytest.mark.parametrize("verb", ["claim-artifact", "release-artifact"])
def test_row_verb_is_one_interpreter_under_the_process_time_bar(repo, verb):
    cmd = [sys.executable, str(_CLI), verb, "row", _ROW, str(repo)]
    result = batched_process_time_ms(cmd, k=_K, env=_env(), cwd=str(repo))
    assert result["rc"] == 0, f"{verb} exited rc={result['rc']}"
    assert result["process_time_ms"] < _BUDGET_MS, result
    assert result["procs_per_call"] == 1, result


def test_row_claim_blocker_spawns_nothing(repo, monkeypatch):
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: True)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-perf")
    assert claims.claim_artifact("row", _ROW, baton_repo_root=str(repo), cwd=str(repo))
    sessions_dir = repo / ".git" / "coordinator-sessions"
    key = claims.row_claim_key(_ROW)
    with _count_spawns_attributed(monkeypatch) as spawns:
        assert claims.row_claim_blocker(sessions_dir, key, "sid-other") == "sid-perf"
        assert claims.row_claim_blocker(sessions_dir, "bug-backlog--absent", "sid-other") is None
    assert list(spawns) == []
