"""The stall probe reproduces each hold in-process, honours its own guard and leaves no state behind."""
import json

import pytest

from coordinator_core import atomic_append
from coordinator_core.benchmarks import engine_stall_repro as probe
from coordinator_core.telemetry import log_rotation

_KEYS = {"scenario", "hold_ms", "cpu_ms", "spawns", "hold_site"}


@pytest.mark.deliberate_wall_clock(reason="the probe measures a caller's blocked time")
def test_every_scenario_emits_a_record_and_the_guard_bounds_the_stalled_sink():
    records = probe.measure(guard_secs=0.5)
    assert [r["scenario"] for r in records] == list(probe.SCENARIOS)
    for r in records:
        assert _KEYS <= set(r)
        assert r["spawns"] == 0
        assert r["hold_site"]
    by = {r["scenario"]: r for r in records}
    assert by["concurrent-append"]["completed"] is True
    assert by["rotation-all-sinks-peer-held"]["sinks"] == len(log_rotation._KNOWN_SINK_NAMES)
    stalled = by["stalled-sink-record-append"]
    assert stalled["hold_ms"] <= 0.5 * 1000 + 500
    assert stalled["line_landed_after_release"] is True


@pytest.mark.deliberate_wall_clock(reason="the probe measures a caller's blocked time")
def test_probe_leaves_no_writer_or_rotation_state_behind():
    probe.measure(guard_secs=0.3, scenarios=("rotation-all-sinks-peer-held", "stalled-sink-record-append"))
    assert not atomic_append.in_flight_sinks()
    assert not log_rotation._in_flight


def test_a_hold_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(
        probe, "_RUNNERS",
        {"stalled-sink-record-append": lambda tmp, g: probe._record("stalled-sink-record-append", 9999.0, 0.0, 0, "x")},
    )
    records = probe.measure(scenarios=("stalled-sink-record-append",))
    assert records[0]["hold_ms"] == 9999.0


def test_main_json_names_scenarios_over_the_bar_and_enforce_bar_sets_exit(monkeypatch, capsys):
    from coordinator_core.telemetry import op_latency

    monkeypatch.setenv(op_latency.ORIGIN_ENV, op_latency.BENCHMARK)
    over = probe._record("a", 900.0, 0.0, 0, "x")
    ok = probe._record("b", 10.0, 0.0, 0, "x")
    monkeypatch.setattr(probe, "measure", lambda guard_secs: [over, ok])
    assert probe.main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["over_bar"] == ["a"]
    assert [r["scenario"] for r in out["records"]] == ["a", "b"]
    assert probe.main(["--json", "--enforce-bar"]) == 1
    capsys.readouterr()
    assert probe.main(["--json", "--enforce-bar", "--bar-ms", "1000"]) == 0
