"""Exact-equality spawn count for `dispatch.ask_gate`'s resume read.

A plan on disk with a row still open costs one `git log` for the checkpoint-landed rows; the
figure is read from the budget manifest, never chosen here.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import _plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _budget() -> dict:
    return load_manifest()["overrides"]["dispatch.ask_gate"]["spawn_count_budget"]


def test_a_resumable_plan_with_an_open_row_spawns_exactly_the_budgeted_checkpoint_log(repo, monkeypatch):  # noqa: F811
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=True, **no_console_creationflags())
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "open"), ("A2", "open")])

    with _count_spawns_attributed(monkeypatch) as spawns:
        verdict = gate(repo, REL)

    assert verdict.halt is None and verdict.resume_rows == ["A1", "A2"], verdict
    assert len(spawns) == _budget()["resume_checkpoint_log"], [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)
    assert all(s.argv[1] == "log" for s in spawns), [s.argv for s in spawns]
