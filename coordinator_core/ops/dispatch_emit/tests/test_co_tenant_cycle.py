"""co_tenant_cycle: need-edge walk over real stage-wait, halt-marker and halted-index records."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import co_tenant_cycle, stage_wait

SHARED = "bin/claude-klabauter-doctor-probe.py"
A = "ask-20261121T100000-aaaaaa"
B = "ask-20261121T101000-bbbbbb"
C = "ask-20261121T102000-cccccc"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _halt(repo: Path, run_id: str, withheld, cause: str = "co_tenant_wait", status: str = "halted") -> None:
    rd = repo / "state" / "warp" / run_id
    rd.mkdir(parents=True)
    marker = {"status": status, "halted": cause, "run_artifacts": {"withheld": {p: "kept" for p in withheld}}}
    (rd / "halted.json").write_text(json.dumps(marker), encoding="utf-8")


def test_two_halted_runs_sharing_a_path_form_a_cycle(repo) -> None:
    _halt(repo, A, (SHARED, "a.py"), cause="co_tenant_dependency_on_halted_run")
    _halt(repo, B, (SHARED, "b.py"))
    assert co_tenant_cycle.cycle_through(repo, B, A) == (B, A)
    assert co_tenant_cycle.cycle_through(repo, A, B) == (A, B)


def test_live_owner_parked_behind_the_needer_closes_the_cycle(repo) -> None:
    stage_wait.park(repo, B, [A], ["p.py"])
    assert co_tenant_cycle.cycle_through(repo, A, B) == (A, B)


def test_three_run_cycle_through_stage_waits(repo) -> None:
    stage_wait.park(repo, B, [C], ["p.py"])
    stage_wait.park(repo, C, [A], ["q.py"])
    assert co_tenant_cycle.cycle_through(repo, A, B) == (A, B, C)


def test_chain_without_back_edge_is_none(repo) -> None:
    stage_wait.park(repo, B, [C], ["p.py"])
    assert co_tenant_cycle.cycle_through(repo, A, B) is None


def test_halted_owner_holding_no_shared_path_is_none(repo) -> None:
    _halt(repo, A, ("a.py",))
    _halt(repo, B, ("b.py",))
    assert co_tenant_cycle.cycle_through(repo, B, A) is None


def test_halted_run_with_a_non_co_tenant_cause_has_no_halted_edge(repo) -> None:
    _halt(repo, A, (SHARED,), cause="test_infra_failure")
    _halt(repo, B, (SHARED,))
    assert co_tenant_cycle.cycle_through(repo, B, A) is None


def test_resumed_marker_has_no_halted_edge(repo) -> None:
    _halt(repo, A, (SHARED,), status="resumed")
    _halt(repo, B, (SHARED,))
    assert co_tenant_cycle.cycle_through(repo, B, A) is None


def test_cycle_not_reaching_the_needer_terminates_as_none(repo) -> None:
    stage_wait.park(repo, B, [C], ["p.py"])
    stage_wait.park(repo, C, [B], ["q.py"])
    assert co_tenant_cycle.cycle_through(repo, A, B) is None


def test_needer_is_never_its_own_owner(repo) -> None:
    stage_wait.park(repo, A, [A], ["p.py"])
    assert co_tenant_cycle.cycle_through(repo, A, A) is None


def test_joint_remedy_is_the_land_run_call_naming_every_run() -> None:
    call = co_tenant_cycle.joint_remedy((B, A))
    assert call == f'coordinator-invoke dispatch.land_run \'{{"joint": ["{B}", "{A}"]}}\''
    assert co_tenant_cycle.CAUSE_CYCLE == "co_tenant_cycle"


def test_walk_spawns_nothing(repo, monkeypatch) -> None:
    _halt(repo, A, (SHARED,))
    _halt(repo, B, (SHARED,))

    def _boom(*_a, **_k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert co_tenant_cycle.cycle_through(repo, B, A) == (B, A)
