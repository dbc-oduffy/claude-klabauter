"""The cold-operation ask_gate split bills a real cold child per leg and enforces the bar on both sides.

Behaviour pinned: the production entry point (`measure`) spawns a real `cold_operation` child on a run-bound
sizing, which binds the run and answers with no error; every promised leg is present and non-negative;
the stamp-gate carve-out is chosen by the engine root's stamp (a stamped root needs none, an unstamped or
empty-stamped one does); `over_bar` refuses a breach, an error reply and a halt, and accepts a clean answer
under the bar; `-X importtime` rows parse into largest-first cumulative entries.
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import cold_operation_ask_gate_split as bench

pytestmark = pytest.mark.cadence

DIRS = 20
LEGS = ("interpreter_start_ms", "imports_ms", "dispatch_ms", "body_ms", "lock_wait_ms")


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path_factory.mktemp("settings-home")))
        return bench.measure(dirs=DIRS, runs=1)


def test_real_cold_child_binds_the_run_and_answers(report):
    assert report["error_code"] is None, report["error_message"]
    assert report["halt"] is None, report
    assert report["bound_sizing_path"].startswith("state/sizings/"), report
    assert report["tree_procs"] >= 1 and isinstance(report["spawns"], int)


def test_every_leg_and_total_is_reported(report):
    for leg in LEGS:
        assert report[leg] >= 0.0, (leg, report)
    assert report["total_process_ms"] > 0 and report["cold_total_process_ms"] > 0
    assert report["dominant_leg"] in LEGS
    assert report["import_top"] and report["import_top"][0]["cumulative_us"] >= report["import_top"][-1]["cumulative_us"]


def test_carveout_follows_the_stamp(tmp_path):
    stamped = tmp_path / "stamped"
    (stamped / "coordinator_core").mkdir(parents=True)
    (stamped / "coordinator_core" / "_engine_stamp").write_text("abc", encoding="utf-8")
    empty = tmp_path / "empty"
    (empty / "coordinator_core").mkdir(parents=True)
    (empty / "coordinator_core" / "_engine_stamp").write_text("", encoding="utf-8")
    assert bench.needs_unstamped_carveout(stamped) is False
    assert bench.needs_unstamped_carveout(empty) is True
    assert bench.needs_unstamped_carveout(tmp_path / "absent") is True


def _clean(**over):
    base = {"total_process_ms": 200.0, "cold_total_process_ms": 240.0, "error_code": None, "halt": None}
    return {**base, **over}


def test_over_bar_accepts_a_clean_answer_under_the_bar():
    assert bench.over_bar(_clean()) is False


@pytest.mark.parametrize(
    "over",
    [
        {"cold_total_process_ms": bench.BAR_MS},
        {"total_process_ms": bench.BAR_MS + 1},
        {"error_code": -32005},
        {"halt": "stage_pending"},
    ],
)
def test_over_bar_refuses_breach_error_and_halt(over):
    assert bench.over_bar(_clean(**over)) is True


def test_parse_importtime_sorts_largest_cumulative_first_and_skips_noise():
    stderr = (
        "import time: self [us] | cumulative | imported package\n"
        "import time:       100 |        100 |   _io\n"
        "import time:       200 |       9000 | coordinator_core.ipc\n"
        "import time:       300 |       5000 |   json\n"
        "noise line\n"
    )
    rows = bench.parse_importtime(stderr, top=2)
    assert [r["module"] for r in rows] == ["coordinator_core.ipc", "json"]
    assert rows[0] == {"module": "coordinator_core.ipc", "cumulative_us": 9000, "self_us": 200}
