"""A 7-plan `roadmap.blitz_land` fire costs under the DR-344 500ms bar of process time and spawns nothing.

Cadence tier: the adaptive timing window re-lands the same wave many times (re-landing is idempotent).
"""

from __future__ import annotations

import time

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.roadmap import blitz_land as bl
from coordinator_core.roadmap.tests._multi_plan_wave_fixture import build_wave, install_fixture_gate

pytestmark = pytest.mark.cadence

BAR_MS = 500.0
N = 7


def test_seven_plan_fire_is_under_the_bar_and_spawn_free(tmp_path, monkeypatch):
    install_fixture_gate(monkeypatch)
    root, wave = build_wave(tmp_path, n=N)

    with _count_spawns_attributed(monkeypatch) as spawns:
        started = time.process_time()
        out = bl.land_wave(root, wave)
        first_land_ms = (time.process_time() - started) * 1000.0
        relanded = in_process_time_ms(lambda: bl.land_wave(root, wave))

    assert len(out["approved"]) == N and out["refused"] == [], out
    assert not spawns, [s.argv for s in spawns]
    assert first_land_ms < BAR_MS, first_land_ms
    assert relanded["process_time_ms"] < BAR_MS, relanded
