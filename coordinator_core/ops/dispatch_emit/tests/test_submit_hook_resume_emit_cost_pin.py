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


#: Past `resume_landed_state`'s `git log -n 500` window: the commit walk is capped there, not at history length.
PRODUCTION_COMMITS = 600
WALK_WINDOW = 500


@pytest.fixture(scope="module")
def production_sized() -> dict:
    return bench.measure(commits=PRODUCTION_COMMITS, top=1)


def test_a_production_sized_history_keeps_the_warm_resume_under_the_bar(production_sized):
    assert production_sized["warm_process_ms"] is not None
    assert production_sized["warm_process_ms"] < bench.BAR_MS, production_sized["warm_process_ms"]
    assert production_sized["git_spawns"] <= SPAWN_CEILING, production_sized["git_spawn_argv"]


def test_a_production_sized_history_bounds_the_commit_walk_at_the_window(production_sized):
    assert production_sized["commit_meta_calls"] <= WALK_WINDOW + 1, production_sized["commit_meta_calls"]


def test_the_repo_replay_drives_evaluate_on_a_halted_sizing_in_a_real_tree(tmp_path):
    """`--repo` mode: the halted sizing is found in the tree and `evaluate` is driven, launch stubbed."""
    bench.build_fixture(tmp_path, 8)
    assert bench.halted_sizings(tmp_path) == [bench.SIZING_REL]
    result = bench.replay(tmp_path, None, top=3)
    assert result["sizing"] == bench.SIZING_REL
    assert result["candidates_tried"] == 1
    assert result["warm_process_ms"] is not None
    assert result["decision"] in ("deny", None)
    assert result["resumes_halted_run"] == (result["reaches_launch"] and result["decision"] != "deny")


def test_the_repo_replay_finds_no_halted_run_in_an_empty_tree(tmp_path):
    assert bench.halted_sizings(tmp_path) == []
    with pytest.raises(SystemExit):
        bench.replay(tmp_path)


def test_the_submit_hook_is_a_resident_engine_op_on_the_warm_blocking_transport():
    """The hook the benchmark measures is reached as a registered engine op over PreToolUse (no interpreter start)."""
    import coordinator_core.hooks.submit_engine_ask  # noqa: F401 -- registers the op
    from coordinator_core.ipc import get_op_handler
    from coordinator_core.warm.hook_http import BLOCKING_EVENTS

    assert get_op_handler("hooks.submit_engine_ask") is not None
    assert "PreToolUse" in BLOCKING_EVENTS
