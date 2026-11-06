"""sizing-assemble on a routed sizing stays under the 500ms process bar, spawns nothing and makes no door hop."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import sizing_assemble_process_budget as B

#: Spawns measured at base c767d4e1e9 (the trampoline runs fully in-process).
SPAWN_FLOOR = 0


@pytest.fixture(scope="module")
def report() -> dict:
    return B.measure(runs=3)


def test_the_scout_shaped_call_exits_clean_on_the_routed_fixture(report):
    assert report["cold_exit_code"] == 0, report["stdout"]
    assert report["exit_code"] == 0, report["stdout"]


def test_cold_and_warm_process_time_are_under_the_bar(report):
    assert report["cold_process_ms"] < B.BAR_MS, report["cold_process_ms"]
    assert report["warm_process_ms"] < B.BAR_MS, report["warm_process_ms"]


def test_spawns_stay_at_or_below_the_floor(report):
    assert report["spawns"] <= SPAWN_FLOOR, report["spawn_argv"]
    assert report["cold_spawns"] <= SPAWN_FLOOR


def test_no_warm_door_hop_inside_sizing_assemble(report):
    # A -32004 is minted only inside warm.client.try_warm_dispatch; none runs on this path,
    # so an indeterminate seen around a scout's sizing-assemble came from outside the call.
    assert report["door_hops"] == 0


def test_a_door_hop_is_counted_when_one_is_made():
    import pytest as _pytest

    from coordinator_core.warm import client

    counts: dict = {}
    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(client, "try_warm_dispatch", lambda *a, **k: "sentinel")
        with B._counted_hops(mp, counts):
            assert client.try_warm_dispatch() == "sentinel"
    finally:
        mp.undo()
    assert counts == {"try_warm_dispatch": 1}
