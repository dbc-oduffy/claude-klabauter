"""A resume seeds rows landed under either spelling of a plan path, so a done dependency does not hold its dependents."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git.checkpoint_guard import checkpoint_plan_matches
from coordinator_core.ops.dispatch_emit.emit import resume_landed_state
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

FLAT = "docs/plans/2026-11-12-photo-inventory-streaming-resume-b2a5e6.md"
SHARD = "docs/plans/2026-11/2026-11-12-photo-inventory-streaming-resume-b2a5e6.md"
OTHER_FLAT = "docs/plans/2026-11-12-some-other-plan-aaaaaa.md"


def _git(repo, *a):
    return subprocess.run(
        ["git", *a], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _checkpoint(repo, name, ids, plan_trailer):
    (repo / name).write_text("x\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(
        repo, "commit", "-q", "-m", f"checkpoint(wave 0): {len(ids)} rows — {', '.join(ids)}",
        "-m", f"Checkpoint-Plan: {plan_trailer}",
    )


def _init(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")


@pytest.mark.parametrize(
    "landed_under,resumed_for", [(FLAT, SHARD), (SHARD, FLAT), (FLAT, FLAT), (SHARD, SHARD)],
    ids=["flat-then-shard", "shard-then-flat", "flat-flat", "shard-shard"],
)
def test_resume_seeds_done_row_under_either_plan_spelling(repo, landed_under, resumed_for):  # noqa: F811
    _init(repo)
    _checkpoint(repo, "c2.txt", ["C2"], landed_under)
    assert "C2" in resume_landed_state(repo, resumed_for).rows


def test_resume_does_not_seed_a_different_plan(repo):  # noqa: F811
    _init(repo)
    _checkpoint(repo, "c2.txt", ["C2"], OTHER_FLAT)
    assert "C2" not in resume_landed_state(repo, SHARD).rows
    assert "C2" not in resume_landed_state(repo, FLAT).rows


def test_plan_matches_pins_both_sides_of_the_equivalence():
    assert checkpoint_plan_matches(FLAT, SHARD) and checkpoint_plan_matches(SHARD, FLAT)
    assert checkpoint_plan_matches(f"repo:{FLAT}", SHARD)
    assert checkpoint_plan_matches(FLAT.replace("/", "\\"), SHARD)
    assert not checkpoint_plan_matches(OTHER_FLAT, SHARD)
    assert not checkpoint_plan_matches(FLAT, "docs/plans/2026-10/" + FLAT.rsplit("/", 1)[-1])
    undated = "docs/plans/undated.md"
    assert checkpoint_plan_matches(undated, undated)
    assert not checkpoint_plan_matches(undated, "docs/plans/2026-11/undated.md")
