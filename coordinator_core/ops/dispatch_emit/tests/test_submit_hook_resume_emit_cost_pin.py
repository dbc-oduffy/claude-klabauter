"""Call/spawn cost pin on the engine-ask submit hook's resume branch (`ask_gate.gate` on a halted run).

Drives `benchmarks.submit_hook_resume_emit.measure` (a real packed git history, a halted
`review_blocked` run record) and pins the git spawn count and the `commit_meta` call count of one
warm resume to the measured base. Process time is not pinned here: it is the benchmark's report, and
a spawn/call count is what a peer's load cannot move. A cut lowers these ceilings; nothing raises them.
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import submit_hook_resume_emit as bench

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

COMMITS = 48
#: `resume_landed_state`'s one `git log`.
SPAWN_CEILING = 1
#: Seed + plan + A1 checkpoint + COMMITS churn walked once by `outside_landings`, plus the terminal read.
META_CEILING = COMMITS + 3


@pytest.fixture(scope="module")
def measured() -> dict:
    return bench.measure(commits=COMMITS, top=5)


def test_the_resume_takes_the_halted_run_not_a_fresh_mint(measured):
    assert measured["resumes_halted_run"], (measured["halt"], measured["resume_plan"], measured["resume_rows"])
    assert measured["resume_rows"] == list(bench.OPEN_ROWS)


def test_the_resume_path_spawns_no_more_git_than_the_pinned_ceiling(measured):
    assert measured["git_spawns"] <= SPAWN_CEILING, measured["git_spawn_argv"]


def test_the_resume_path_reads_no_more_commits_than_the_pinned_ceiling(measured):
    assert measured["commit_meta_calls"] <= META_CEILING, measured["commit_meta_calls"]
