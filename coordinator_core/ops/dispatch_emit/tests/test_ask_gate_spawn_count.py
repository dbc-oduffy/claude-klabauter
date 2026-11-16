"""Exact-equality spawn count for `dispatch.ask_gate`'s resume read.

A plan on disk with a row still open reads the landed-trailer record in process: no git spawn once
the record is seeded, exactly one `git log` to seed it when none exists. Both figures are read from
the budget manifest, never chosen here.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit import landed_trailer_record
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import _plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _budget() -> dict:
    return load_manifest()["overrides"]["dispatch.ask_gate"]["spawn_count_budget"]


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=True, **no_console_creationflags())


def _resumable_repo(repo):  # noqa: F811
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "open"), ("A2", "open")])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")


def test_a_resumable_plan_with_an_open_row_spawns_exactly_the_budgeted_checkpoint_log(repo, monkeypatch):  # noqa: F811
    """No record on disk: the gate spawns the budgeted seeding `git log` and nothing else."""
    _resumable_repo(repo)
    assert landed_trailer_record.trailer_entries(repo) is None

    with _count_spawns_attributed(monkeypatch) as spawns:
        verdict = gate(repo, REL)

    assert verdict.halt is None and verdict.resume_rows == ["A1", "A2"], verdict
    assert len(spawns) == _budget()["resume_checkpoint_seed_log"], [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)
    assert all("log" in s.argv[:4] for s in spawns), [s.argv for s in spawns]


def test_a_resumable_plan_with_a_seeded_record_spawns_no_git(repo, monkeypatch):  # noqa: F811
    """The seeded record serves the landed-state read: the budgeted spawn count is zero."""
    _resumable_repo(repo)
    gate(repo, REL)
    assert landed_trailer_record.trailer_entries(repo) is not None

    with _count_spawns_attributed(monkeypatch) as spawns:
        verdict = gate(repo, REL)

    assert verdict.halt is None and verdict.resume_rows == ["A1", "A2"], verdict
    assert len(spawns) == _budget()["resume_checkpoint_log"], [s.argv for s in spawns]
    assert not spawns, [s.argv for s in spawns]
