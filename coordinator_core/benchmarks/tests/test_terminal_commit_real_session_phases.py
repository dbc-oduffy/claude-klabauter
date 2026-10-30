"""`--phases` of the real-session-shape driver: the per-leg table partitions the call and names the legs."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import terminal_commit_real_session_shape as shape
from coordinator_core.ops.dispatch_emit import process_artifact_policy, terminal_commit

_PRE_COMMIT_LEGS = (
    "locate_run_dir",
    "contested_by_live_peers",
    "_checkin_bytes",
    "_pre_commit_gates",
    "phase:head_classification",
    "phase:pre_commit_gates",
)


def _burn(ms: float) -> None:
    import time

    end = time.process_time() + ms / 1000.0
    while time.process_time() < end:
        pass


def test_nested_legs_partition_the_total_and_inclusive_adds_the_children_back():
    timer = shape.PhaseTimer()
    with timer.measuring():
        with timer.leg("outer"):
            _burn(30)
            with timer.leg("inner"):
                _burn(30)
    assert sum(timer.exclusive_ms.values()) == pytest.approx(timer.total_ms, abs=1.0)
    assert timer.inclusive_ms["outer"] >= timer.exclusive_ms["outer"] + timer.exclusive_ms["inner"] - 1.0
    assert timer.exclusive_ms["inner"] >= 25.0
    assert timer.calls == {"outer": 1, "inner": 1}


@pytest.fixture
def small_landing(tmp_path, monkeypatch):
    for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME", "COORDINATOR_SESSION_ID"):
        monkeypatch.setenv(key, "restored-by-monkeypatch")
    monkeypatch.setattr(process_artifact_policy, "hosts_coordinator_state", lambda root: True)
    monkeypatch.setattr(terminal_commit, "_launch_close_job", lambda *a, **k: True)
    return shape.build_fixture(tmp_path, wf_dirs=3, landing_paths=40)


def test_phases_run_lands_and_every_pre_commit_leg_is_a_row(small_landing):
    repo, params = small_landing
    out, timer, stat = shape.run_phases(repo, params)

    assert out.get("committed") is True and out.get("sha")
    legs = {r["leg"] for r in timer.rows()}
    assert set(_PRE_COMMIT_LEGS) <= legs, legs
    assert sum(timer.exclusive_ms.values()) == pytest.approx(timer.total_ms, abs=1.0)
    assert sum(timer.spawns.values()) == timer.total_spawns
    table = shape.format_table(timer, stat)
    assert "_checkin_bytes" in table and "stat-match" in table


def test_fresh_landed_files_do_not_stat_match_an_index_that_lacks_them(small_landing):
    repo, _ = small_landing
    rels = sorted(f"pkg/{p.name}" for p in (repo / "pkg").glob("*.py"))
    stat = shape.index_stat_match(repo, rels)
    assert stat == {"landed": 40, "in_index": 0, "stat_match": 0}


def test_probes_are_removed_after_the_run(small_landing):
    from coordinator_core.git import commit as git_commit
    from coordinator_core.ops.ceremony import commit_v2_phases

    before = (git_commit._checkin_bytes, commit_v2_phases.PhaseLedger.phase)
    repo, params = small_landing
    shape.run_phases(repo, params)
    assert (git_commit._checkin_bytes, commit_v2_phases.PhaseLedger.phase) == before
