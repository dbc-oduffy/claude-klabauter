"""land_deadline.scope entry itself costs no spawn and little process time; work done before the
first check stays charged to the scope_entry leg (no cost leaves the ledger)."""

from __future__ import annotations

import time

from coordinator_core.ops.dispatch_emit import land_deadline

SCOPE_ENTRY_ALLOTMENT_MS = 40.0


def _leg(costs: list, name: str) -> dict:
    return next(c for c in costs if c["leg"] == name)


def test_entering_a_scope_spawns_nothing_and_fits_the_allotment():
    with land_deadline.scope(budget_secs=None):
        pass
    start = time.thread_time()
    with land_deadline.scope(budget_secs=None):
        costs = land_deadline.leg_costs()
    spent_ms = (time.thread_time() - start) * 1000.0
    entry = _leg(costs, land_deadline.SCOPE_LEG)
    assert entry["spawns"] == 0
    assert spent_ms < SCOPE_ENTRY_ALLOTMENT_MS


def test_work_before_the_first_check_is_charged_to_scope_entry():
    with land_deadline.scope(budget_secs=None):
        deadline = time.thread_time() + 0.02
        while time.thread_time() < deadline:
            pass
        land_deadline.check("land_run.next")
        costs = land_deadline.leg_costs()
    entry = _leg(costs, land_deadline.SCOPE_LEG)
    assert entry["process_time_ms"] >= 15.0
    assert _leg(costs, "land_run.next")["process_time_ms"] < entry["process_time_ms"]


def test_a_mark_moves_the_following_span_into_its_own_leg_and_keeps_the_cost():
    with land_deadline.scope(budget_secs=None):
        land_deadline.mark("stage.a")
        deadline = time.thread_time() + 0.02
        while time.thread_time() < deadline:
            pass
        costs = land_deadline.leg_costs()
    assert _leg(costs, land_deadline.SCOPE_LEG)["process_time_ms"] < SCOPE_ENTRY_ALLOTMENT_MS
    assert _leg(costs, "stage.a")["process_time_ms"] >= 15.0


def test_a_landing_attributes_its_prefix_to_named_legs_not_scope_entry(tmp_path, monkeypatch):
    from coordinator_core.benchmarks import terminal_commit_real_session_shape as shape
    from coordinator_core.ops.dispatch_emit import process_artifact_policy, terminal_commit

    for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME", "COORDINATOR_SESSION_ID"):
        monkeypatch.setenv(key, "restored-by-monkeypatch")
    monkeypatch.setattr(process_artifact_policy, "hosts_coordinator_state", lambda root: True)
    monkeypatch.setattr(terminal_commit, "_launch_close_job", lambda *a, **k: True)
    repo, params = shape.build_fixture(tmp_path, wf_dirs=3, landing_paths=40)

    at_first_check: list = []
    real_check = land_deadline.check

    def spy(leg: str) -> None:
        if not at_first_check:
            at_first_check.extend(land_deadline.leg_costs())
        real_check(leg)

    monkeypatch.setattr(land_deadline, "check", spy)
    out = shape.land(repo, params)

    assert out.get("committed") is True
    names = [c["leg"] for c in at_first_check]
    assert names[0] == land_deadline.SCOPE_LEG
    assert names[1] == "terminal_commit.params"
    assert {"terminal_commit.declared_dirs", "terminal_commit.review_floor", "terminal_commit.snapshot",
            "terminal_commit.tracked_case", "terminal_commit.changed"} <= set(names)
    entry = at_first_check[0]
    assert entry["spawns"] == 0
    assert entry["process_time_ms"] < SCOPE_ENTRY_ALLOTMENT_MS
