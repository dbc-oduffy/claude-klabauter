"""resume_landed_state and sizing_fire.resumable_plan read the landed-trailer record: a seeded record
means zero git spawns and an answer equal to the spawned one; no record means one seeding spawn."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit import landed_trailer_record, sizing_fire
from coordinator_core.ops.dispatch_emit.emit import resume_landed_state
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_resume_landed_state_row_shas import _commit, _git, _init
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import _landed_repo, _load, _put_s
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL as GATE_PLAN_REL

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_REL = "docs/plans/p.md"


def _seeded(repo):  # noqa: F811
    _init(repo)
    _commit(repo, "a.txt", "checkpoint(wave 0): 1 rows — A1", f"Checkpoint-Plan: {PLAN_REL}")


def test_a_seeded_record_answers_with_no_git_spawn_after_a_new_checkpoint_commit(repo, monkeypatch):  # noqa: F811
    _seeded(repo)
    seed = resume_landed_state(repo, PLAN_REL)
    assert landed_trailer_record.trailer_entries(repo) is not None
    sha = _commit(repo, "b.txt", "checkpoint(wave 1): 1 rows — A2", f"Checkpoint-Plan: {PLAN_REL}")

    with _count_spawns_attributed(monkeypatch) as spawns:
        state = resume_landed_state(repo, PLAN_REL)

    assert not spawns, [s.argv for s in spawns]
    assert state.rows == {"A1", "A2"} and seed.rows == {"A1"}
    assert dict(state.row_shas)["A2"] == sha

    landed_trailer_record.record_path(repo).unlink()
    assert resume_landed_state(repo, PLAN_REL) == state


def test_no_record_spawns_exactly_one_git_log_and_seeds_it(repo, monkeypatch):  # noqa: F811
    _seeded(repo)
    assert landed_trailer_record.trailer_entries(repo) is None

    with _count_spawns_attributed(monkeypatch) as spawns:
        resume_landed_state(repo, PLAN_REL)
    assert len(spawns) == 1 and "log" in spawns[0].argv[:4], [s.argv for s in spawns]
    assert landed_trailer_record.trailer_entries(repo) is not None


def test_resumable_plan_spawns_no_git_with_a_seeded_record_and_matches_the_spawned_answer(repo, monkeypatch):  # noqa: F811
    _put_s(repo)
    _landed_repo(repo, landed=["A1"])
    assert [r for r in sizing_fire.resumable_plan(_load(repo), REL, repo, "s").incomplete] == ["A2"]
    _git(repo, "commit", "-q", "--allow-empty", "-m", "checkpoint(wave 2): 1 rows — A2", "-m", f"Checkpoint-Plan: {GATE_PLAN_REL}")

    with _count_spawns_attributed(monkeypatch) as spawns:
        from_record = sizing_fire.resumable_plan(_load(repo), REL, repo, "s")
    assert not spawns, [s.argv for s in spawns]

    landed_trailer_record.record_path(repo).unlink()
    assert sizing_fire.resumable_plan(_load(repo), REL, repo, "s") == from_record
