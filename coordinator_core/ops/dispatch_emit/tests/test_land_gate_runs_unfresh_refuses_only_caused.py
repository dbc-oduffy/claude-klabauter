"""dispatch.land_run with two owed repo-wide invariants, one red at the run base: the gate runs the node
nothing measured through the detached retest, lands when the run caused no red and reports the base-red
node, and refuses only a node green at base and red with the run's hunks."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import (  # noqa: F401
    RUN,
    _author,
    _declare,
    _git,
    _land,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BASE_RED = "coordinator_core/session/tests/test_machinery_paths.py::test_every_tracked_state_first_segment_is_on_the_allowlist"
UNRUN = "coordinator_core/tests/test_no_hand_built_record_home_joins.py::test_no_new_hand_built_record_home_joins"


@pytest.fixture
def owed(repo, monkeypatch):
    monkeypatch.setattr(imp, "invariants_for_paths", lambda paths, root: [BASE_RED, UNRUN])
    monkeypatch.setattr(imp, "existing_invariants", lambda root, nodes: list(nodes))
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])
    return repo


@pytest.fixture
def spawned(owed, monkeypatch):
    from coordinator_core.ops.dispatch_emit import retest_targeted
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import params_path

    calls: list = []
    monkeypatch.setattr(retest_targeted, "spawn_detached", lambda *a, **k: calls.append(a) or True)
    path = params_path(owed, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": RUN, "script_path": ""}), encoding="utf-8")
    return calls


def _record(root, status, tests, **extra):
    record = {
        "run_id": RUN, "head": _git(["rev-parse", "HEAD"], root).strip(), "status": status,
        "tests": tests, "tests_run": len(tests), "covered": landing_retest.landing_fingerprint(root, ["a.py"]),
        **extra,
    }
    path = landing_retest.fresh_verdict_path(root, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_an_unrun_node_beside_a_base_red_one_is_run_then_the_landing_commits_reporting_the_base_red(owed, spawned):
    head = _git(["rev-parse", "HEAD"], owed).strip()

    first = _land(owed, failing_at_base=[BASE_RED])

    assert first["committed"] is False and first["awaiting"]["awaiting"] == "retest", first
    assert first["refused"] is None and first["remedy_step"] is None, first
    assert len(spawned) == 1 and UNRUN in spawned[0][2] and BASE_RED not in spawned[0][2]
    assert "poll" in first["awaiting"] and first["error"].endswith(" --poll"), first
    record = landing_retest._retest_record(owed, RUN)
    assert record["status"] == landing_retest.RETEST_RUNNING and record["tests"] == [UNRUN]
    assert _git(["rev-parse", "HEAD"], owed).strip() == head

    _record(owed, "pass", [UNRUN.split("::")[0]])
    assert landing_retest.fresh_verdict_path(owed, RUN).is_file()
    out = _land(owed, failing_at_base=[BASE_RED])

    assert out["committed"] is True, out
    assert len(spawned) == 1
    assert BASE_RED in json.dumps(out.get("advisory")), out
    assert "a.py" in _git(["show", "--name-only", "--format=", "HEAD"], owed).split()


def test_a_node_green_at_base_and_red_with_the_runs_hunks_refuses_naming_it(owed, spawned):
    _record(
        owed, "fail", [UNRUN.split("::")[0]], tests_failed=1, failed_tests=[UNRUN],
        baseline={"method": "isolated-export", "caused": [UNRUN], "pre_existing": [], "unverified": [],
                  "foreign": [], "evidence": []},
    )
    head = _git(["rev-parse", "HEAD"], owed).strip()

    out = _land(owed, failing_at_base=[BASE_RED], retest_attempts=MAX_IN_RUN_REMEDY_ATTEMPTS)

    assert out["committed"] is False and out["refused"] == "targeted-tests-failed", out
    assert UNRUN in out["error"], out
    assert BASE_RED not in out["error"], out
    assert spawned == []
    assert _git(["rev-parse", "HEAD"], owed).strip() == head
