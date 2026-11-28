"""Standing gate: `emit-dispatch-workflow --plan` through the door to a warm engine, under the 500ms bar.

Reads `coordinator_core.benchmarks.emit_worker_e2e.measure`: the door process's tree process time plus the
warm engine's per-request CPU, per call, never wall clock, and the git children the engine tree starts
(`git_spawns`, counted apart from job-accounted `procs`). Both the fixture repo and this repo's full tracked
test population (`--real-repo`) are held to `BUDGET_MS` and zero git spawns.

THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.

"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import emit_worker_e2e

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BUDGET_MS = emit_worker_e2e.BUDGET_MS


@pytest.fixture(scope="module", params=[False, True], ids=["fixture", "real-repo"])
def emit_measurement(request):
    real_repo = request.param
    try:
        emit_worker_e2e.door_path()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    kwargs = (
        {"samples": emit_worker_e2e.REAL_SAMPLES, "k": emit_worker_e2e.REAL_K_INVOCATIONS}
        if real_repo
        else {"samples": emit_worker_e2e.SAMPLES, "k": emit_worker_e2e.K_INVOCATIONS}
    )
    try:
        reading = emit_worker_e2e.measure(real_repo=real_repo, **kwargs)
    except NotImplementedError as exc:
        pytest.skip(str(exc))
    print(
        f"EMIT-DISPATCH-BUDGET {reading['mode']}: process_ms={reading['process_ms']} "
        f"(door {reading['door_ms']} + worker {reading['worker_ms']}) git_spawns={reading['git_spawns']} "
        f"procs={reading['procs']} path={reading['path']}"
    )
    return reading


def test_emit_dispatch_plan_route_is_warm_door(emit_measurement):
    assert emit_measurement["path"] == emit_worker_e2e.PATH_WARM_DOOR
    assert emit_measurement["process_ms"] > 0, f"call cost not observed: {emit_measurement}"


def test_emit_dispatch_plan_route_is_under_the_500ms_bar(emit_measurement):
    assert emit_measurement["process_ms"] < BUDGET_MS, (
        f"over the {BUDGET_MS}ms bar ({emit_measurement['mode']}): {emit_measurement['sample_process_ms']}"
    )


def test_emit_dispatch_plan_route_spawns_no_git(emit_measurement):
    assert emit_measurement["git_spawns"] == 0, (
        f"{emit_measurement['git_spawns']} git spawns ({emit_measurement['mode']})"
    )
