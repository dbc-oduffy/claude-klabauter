"""Pins for the cold guard path cost instrument: verdict classification (both sides), billing arithmetic, the
report keys dependants read, the respawn blocker, reuse of the sibling instruments' hook payload, and the
non-Windows refusal.

The live measurement (`cold_guard_path_cost.py`) clones the engine and spawns the installed door; it is an
operator run, not a unit test."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.benchmarks import cold_guard_path_cost as mod
from coordinator_core.benchmarks import door_hook_read_latency as latency

REPO_ROOT = Path(__file__).resolve().parents[3]


def _leg(ms: float, procs: float = 1.0) -> dict:
    return {"process_time_ms": ms, "procs_per_call": procs, "samples_ms": [ms], "rc": 0}


def _legs() -> dict:
    return {
        "floor": _leg(40.0, 1.0),
        "door": _leg(640.0, 6.0),
        "cold_entry": _leg(1140.0, 4.0),
        "interpreter": _leg(30.0, 1.0),
        "import": _leg(330.0, 1.0),
        "evaluate_cold": _leg(430.0, 1.0),
    }


def test_a_loud_pass_or_empty_stdout_is_not_a_real_verdict_and_an_allow_is():
    allow = '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
    assert mod.is_real_verdict(allow)
    assert not mod.is_real_verdict("")
    assert not mod.is_real_verdict("coordinator-door: engine unreachable and the cold guard returned no verdict")
    assert not mod.is_real_verdict('{"systemMessage":"guard did not run"}')
    assert not mod.is_real_verdict('{"systemMessage":"guard not evaluated"}')


def test_net_cost_subtracts_the_wrapper_floor_and_clamps_at_zero():
    net = mod.net_cost(_leg(640.0, 6.0), _leg(40.0, 1.0))
    assert net == {"process_ms": 600.0, "spawns": 5.0}
    assert mod.net_cost(_leg(10.0, 1.0), _leg(40.0, 1.0)) == {"process_ms": 0.0, "spawns": 0.0}


def test_median_leg_takes_the_median_of_batches():
    leg = mod.median_leg([_leg(100.0, 2.0), _leg(300.0, 2.0), _leg(200.0, 3.0)])
    assert leg["process_time_ms"] == 200.0 and leg["procs_per_call"] == 2.0
    assert leg["samples_ms"] == [100.0, 300.0, 200.0]


def test_report_carries_the_keys_dependants_read_and_splits_the_cold_leg():
    verdict = {"real": False, "rc": 0, "stdout_head": "coordinator-door: ..."}
    report = mod.build_report(_legs(), verdict, k=3, samples=3)
    for key in ("process_ms", "spawns", "decomposition", "verdict_real", "under_bar", "door_call"):
        assert key in report
    assert report["process_ms"] == 600.0 and report["spawns"] == 5.0
    assert report["verdict_real"] is False
    deco = report["decomposition"]
    assert deco["interpreter_ms"] == 30.0
    assert deco["import_ms"] == 300.0
    assert deco["evaluate_cold_ms"] == 100.0
    assert deco["cold_entry_ms"] == 1100.0 and deco["cold_entry_over_bar"] is True
    assert deco["door_overhead_ms"] == 0.0


def test_the_bar_is_strict_on_both_sides():
    under = mod.build_report({**_legs(), "door": _leg(499.0 + 40.0)}, {"real": True}, 1, 1)
    over = mod.build_report({**_legs(), "door": _leg(500.0 + 40.0)}, {"real": True}, 1, 1)
    assert under["under_bar"] is True and over["under_bar"] is False


def test_block_server_spawn_leaves_a_file_where_the_service_directory_parent_goes(tmp_path):
    runtime = tmp_path / "runtime"
    mod.block_server_spawn(runtime)
    blocker = runtime / mod.RUNTIME_BLOCKER
    assert blocker.is_file()
    with pytest.raises((NotADirectoryError, FileNotFoundError, OSError)):
        (blocker / "warm" / "hash").mkdir(parents=True)


def test_the_hook_payload_and_entrypoint_are_the_latency_instruments_not_forks():
    assert mod.hook_payload is latency.hook_payload
    assert mod.ENTRYPOINT == latency.ENTRYPOINT
    assert mod.is_fail_open is latency.is_fail_open


def test_door_environment_pins_hook_mode_root_and_private_homes(tmp_path):
    env = mod.door_environment(tmp_path / "root", tmp_path / "rt", tmp_path / "st")
    assert env["COORDINATOR_DOOR_STDIN_MODE"] == "hook"
    assert env["COORDINATOR_DOOR_ENGINE_ROOT"] == str(tmp_path / "root")
    assert env["COORDINATOR_ENGINE_ROOT"] == str(tmp_path / "root")
    assert env["COORDINATOR_WARM_RUNTIME_BASE"] == str(tmp_path / "rt")
    assert env["COORDINATOR_SETTINGS_HOME"] == str(tmp_path / "st")


def test_fail_open_log_reads_the_private_settings_home(tmp_path):
    assert mod.read_fail_open_log(tmp_path) == {"lines": 0, "last": None}
    log = tmp_path / "state" / "door-fail-open.log"
    log.parent.mkdir()
    log.write_text("a\nlast line\n", encoding="utf-8")
    assert mod.read_fail_open_log(tmp_path) == {"lines": 2, "last": "last line"}


@pytest.mark.skipif(os.name == "nt", reason="the refusal is the non-Windows path")
def test_run_refuses_off_windows(tmp_path):
    with pytest.raises(SystemExit):
        mod.run(REPO_ROOT, tmp_path / "door", 1, 1)
