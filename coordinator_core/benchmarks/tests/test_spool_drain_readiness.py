"""The spool-drain readiness benchmark splits spool-to-started-run into legs by process time and names the wait.

The pure split runs on synthetic stamps; `measure` drives the real `_DrainTrigger` and `drain.run` under `tmp_path`
with a stub evaluate and fire. The throwaway-engine mode is the script's own run, not a pin here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import fixture_settings_home
from coordinator_core.benchmarks import spool_drain_readiness as B


def _stamp(event: str, wall: float, cpu: float, thread_cpu: float = 0.0, spawns: int = 0, thread: int = 1) -> dict:
    return {"event": event, "wall": wall, "cpu": cpu, "thread_cpu": thread_cpu, "spawns": spawns, "thread": thread}


def _full(idle_s: float = 0.0) -> list:
    """One request, the drain spawned `idle_s` after the spool write, a mint, a fire; CPU figures small."""
    t = 100.0 + idle_s
    return [
        _stamp("request", t, 10.010),
        _stamp("maybe_drain", t, 10.010),
        _stamp("drain_spawn", t, 10.012, spawns=1),
        _stamp("drain_thread_start", t + 0.01, 10.020, thread_cpu=0.000, spawns=1),
        _stamp("mint_start", t + 0.02, 10.030, thread_cpu=0.010, spawns=1),
        _stamp("mint_end", t + 0.32, 10.330, thread_cpu=0.310, spawns=3),
        _stamp("fire_start", t + 0.33, 10.340, thread_cpu=0.320, spawns=3),
        _stamp("fire_end", t + 0.34, 10.350, thread_cpu=0.330, spawns=4),
        _stamp("drain_end", t + 0.35, 10.360, thread_cpu=0.340, spawns=4),
    ]


def _legs(stamps: list) -> dict:
    return B.legs_from_stamps(stamps, spool_ms=2.0, spool_wall_ms=2.0, spool_wall=100.0, engine_cpu_ms=10_000.0,
                              base_spawns=0)


def test_legs_are_process_ms_with_a_total_spawns_and_the_largest_leg():
    got = _legs(_full())
    assert got["started"] and got["within_budget"]
    assert got["legs"]["evaluate"] == pytest.approx(300.0, abs=0.5)
    assert got["legs"]["fire"] == pytest.approx(10.0, abs=0.5)
    assert got["legs"]["spool_write"] == 2.0
    assert got["legs"]["total"] == pytest.approx(sum(got["legs"][k] for k in B.LEGS), abs=0.5)
    assert got["largest_leg"] == "evaluate"
    assert got["spawns"]["evaluate"] == 2 and got["spawns"]["total"] == 4
    assert got["released_by"]["trigger_wait"] == "request#1"


def test_an_idle_gap_is_invisible_to_process_time_but_named_as_the_largest_wait():
    got = _legs(_full(idle_s=25.0))
    assert got["legs"]["trigger_wait"] < 100.0, "an idle gap burns no CPU"
    assert got["wall_ms_diag"]["trigger_wait"] == pytest.approx(25_000.0, abs=1.0)
    assert got["largest_wait_leg"] == "trigger_wait"
    assert got["largest_leg"] != "trigger_wait"
    assert got["requests_before_drain_start"] == 1


def test_a_drain_that_never_spawns_reports_no_start_and_no_budget_pass():
    got = _legs([_stamp("request", 100.0, 10.0)])
    assert got["started"] is False and got["within_budget"] is False
    assert got["requests_before_drain_start"] is None and got["legs"]["trigger_wait"] is None


def test_a_mint_with_no_fire_is_not_a_started_run():
    got = _legs([s for s in _full() if not s["event"].startswith("fire")])
    assert got["started"] is False and got["legs"]["fire"] is None and got["legs"]["evaluate"] is not None


def test_a_total_over_the_budget_and_a_leg_over_the_bar_are_named():
    stamps = _full()
    stamps[-3]["thread_cpu"] = 0.320 + 11.0
    stamps[-2]["thread_cpu"] = 0.330 + 11.0
    got = _legs(stamps)
    assert got["within_budget"] is False and got["over_bar_legs"] == ["run_started"]
    assert _legs(_full())["over_bar_legs"] == [] and _legs(_full())["within_budget"] is True


def test_measure_drives_the_real_trigger_and_drain_and_restores_the_server(tmp_path):
    from coordinator_core.warm import server

    originals = (server._spawn_background, server._run_ask_spool_drain, server._DrainTrigger.maybe_drain)
    repo = tmp_path / "repo"
    repo.mkdir()
    minted: list = []
    inner = B.scripting_evaluate(repo)

    def evaluate(payload: dict) -> dict:
        minted.append(payload["tool_use_id"])
        return inner(payload)

    with fixture_settings_home(tmp_path):
        got = B.measure(repo, evaluate=evaluate)
    assert got["started"] and got["mode"] == "inproc"
    assert minted == ["toolu_spool_drain_bench"]
    assert got["idle_drains_started"] == 0, "nothing starts a drain without a request"
    assert got["requests_before_drain_start"] == 1
    assert set(got["legs"]) == {*B.LEGS, "total"}
    assert all(got["legs"][k] is not None for k in B.LEGS)
    assert originals == (server._spawn_background, server._run_ask_spool_drain, server._DrainTrigger.maybe_drain)
    json.dumps(got)


def test_measure_with_a_refusing_evaluate_does_not_start_a_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def refuse(_payload: dict) -> dict:
        return {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "no"}}

    with fixture_settings_home(tmp_path):
        got = B.measure(repo, evaluate=refuse)
    assert got["started"] is False and got["within_budget"] is False


def test_the_stamp_snippet_has_a_seam_in_the_live_server_and_refuses_one_without():
    server = Path(B.__file__).resolve().parents[1] / "warm" / "server.py"
    text = B.instrumented_server_source(server.read_text(encoding="utf-8"))
    assert text.rstrip().endswith(B._SERVER_TAIL.rstrip()) and "install_engine_stamps" in text
    with pytest.raises(ValueError):
        B.instrumented_server_source("print('no guard')\n")


def test_render_prints_every_leg_the_total_and_largest_leg():
    lines = B.render({**_legs(_full()), "idle_drains_started": 0, "idle_probe_s": 2.0})
    text = "\n".join(lines)
    assert all(f"leg {leg}:" in text for leg in B.LEGS) and "largest_leg=evaluate" in text and "total:" in text
