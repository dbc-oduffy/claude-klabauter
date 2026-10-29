"""Cost pin on the engine-ask submit hook's halted-run resume (the halt-time record path).

Drives `benchmarks.submit_hook_resume_emit.measure` (a real packed git history, a halted
`review_blocked` run with its `resume.json`) through `submit_engine_ask.evaluate` and pins the hook's
git spawns and `commit_meta` calls to zero and its warm process time under the ruling's R1 bar. Spawn
and call counts are what a peer's load cannot move; the warm bar is the ruling's. A cut lowers these
ceilings; nothing raises them.
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import submit_hook_resume_emit as bench

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

COMMITS = 48
#: The ruling's R1: a halted-run resume submit costs under this much process time, warm.
RESUME_BAR_MS = 300.0
SPAWN_CEILING = 0
META_CEILING = 0


@pytest.fixture(scope="module")
def measured() -> dict:
    return bench.measure(commits=COMMITS, top=5)


def test_the_resume_takes_the_halted_run_from_its_record(measured):
    assert measured["resumes_halted_run"], (measured["decision"], measured["reason"])
    assert measured["record_hit"], measured["reason"]


def test_the_resume_hook_spawns_no_git(measured):
    assert measured["hook_git_spawns"] <= SPAWN_CEILING, measured["git_spawn_argv"]
    assert measured["git_spawns"] <= SPAWN_CEILING, measured["git_spawn_argv"]


def test_the_resume_hook_reads_no_commits(measured):
    assert measured["commit_meta_calls"] <= META_CEILING, measured["commit_meta_calls"]


def test_the_warm_resume_stays_under_the_ruling_bar(measured):
    assert measured["warm_process_ms"] is not None
    assert measured["warm_process_ms"] < RESUME_BAR_MS, measured["warm_process_ms"]


#: Past the old `resume_landed_state` window (`git log -n 500`): history length must not reach the hook.
PRODUCTION_COMMITS = 600


@pytest.fixture(scope="module")
def production_sized() -> dict:
    return bench.measure(commits=PRODUCTION_COMMITS, top=1)


def test_a_production_sized_history_keeps_the_warm_resume_under_the_bar(production_sized):
    assert production_sized["record_hit"], production_sized["reason"]
    assert production_sized["warm_process_ms"] is not None
    assert production_sized["warm_process_ms"] < RESUME_BAR_MS, production_sized["warm_process_ms"]
    assert production_sized["hook_git_spawns"] <= SPAWN_CEILING, production_sized["git_spawn_argv"]
    assert production_sized["commit_meta_calls"] <= META_CEILING, production_sized["commit_meta_calls"]


def test_the_repo_replay_drives_evaluate_on_a_halted_sizing_in_a_real_tree(tmp_path):
    """`--repo` mode: the halted sizing is found in the tree and `evaluate` is driven, launch stubbed."""
    bench.build_fixture(tmp_path, 8)
    assert bench.halted_sizings(tmp_path) == [bench.SIZING_REL]
    result = bench.replay(tmp_path, None, top=3)
    assert result["sizing"] == bench.SIZING_REL
    assert result["candidates_tried"] == 1
    assert result["warm_process_ms"] is not None
    assert result["decision"] == "allow"
    assert result["record_hit"]
    assert result["resumes_halted_run"]


def test_a_missing_record_starts_the_run_through_the_launcher_without_composing(tmp_path, monkeypatch):
    """The miss side of the decision: no `resume.json` -> the recompose is scheduled and the run still starts."""
    from coordinator_core.hooks import submit_engine_ask as hook
    from coordinator_core.ops.dispatch_emit import ask_compose

    bench.build_fixture(tmp_path, 4)
    (tmp_path / "state" / "warp" / bench.RUN_ID / "resume.json").unlink()
    scheduled: list = []
    monkeypatch.setattr(hook, "_spawn_recompose", lambda root, run_id: scheduled.append(run_id) or True)
    monkeypatch.setattr(ask_compose, "compose_ask_script", lambda *a, **k: pytest.fail("composed inline on a resume"))
    result = bench.replay(tmp_path, bench.SIZING_REL, top=1, cold_only=True)
    assert scheduled == [bench.RUN_ID]
    assert not result["record_hit"]
    assert result["resumes_halted_run"]


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
