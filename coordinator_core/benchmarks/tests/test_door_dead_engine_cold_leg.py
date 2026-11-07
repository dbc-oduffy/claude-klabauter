"""Pins for the dead-engine door instrument: billing arithmetic, candidate choice, source export at a pinned
ref, the hardlink-safe cold-entry swap, process cleanup, and the Windows arm's platform refusal.

The live measurement itself (`door_dead_engine_cold_leg.py --door posix`) builds a door and boots servers;
it is an operator run, not a unit test."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import door_dead_engine_cold_leg as mod

REPO_ROOT = Path(__file__).resolve().parents[3]


def _leg(door_p90: float, wait_ms: float = 0.0) -> dict:
    return {"door_ms": {"p90": door_p90}, "boot_wait_billed_ms": wait_ms}


def _report(boot_wall_p90: float = 4200.0, warm_p90: float = 3.0, baseline_wait: float = 2000.0) -> dict:
    return {
        "baseline": _leg(190.0, baseline_wait),
        "slim_cold": _leg(185.0, 2000.0),
        "cold_no_wait": _leg(192.0, 0.0),
        "fail_fast": _leg(30.0),
        "bounded_wait": {"boot_ready_wall_ms": {"p90": boot_wall_p90}, "warm_call_ms": {"p90": warm_p90}},
    }


def test_percentile_is_nearest_rank_and_refuses_empty():
    assert mod.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 50) == 3.0
    assert mod.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 90) == 5.0
    with pytest.raises(ValueError):
        mod.percentile([], 50)


def test_summarize_reports_p50_p90_and_survives_no_samples():
    s = mod.summarize([10.0, 20.0, 30.0, 40.0, 50.0])
    assert (s["n"], s["min"], s["p50"], s["p90"], s["max"]) == (5, 10.0, 30.0, 50.0, 50.0)
    assert mod.summarize([])["p90"] is None


def test_wait_budget_rounds_up_to_the_step():
    assert mod.wait_budget_ms(251.2) == 300.0
    assert mod.wait_budget_ms(300.0) == 300.0
    assert mod.wait_budget_ms(300.1) == 350.0


def test_a_sleeping_wait_is_billed_at_full_value_not_zero_cpu():
    bill = mod.bill_candidate("bounded_wait", process_p90_ms=3.0, wait_ms=450.0)
    assert bill["billed_ms"] == 453.0 and bill["under_bar"]
    over = mod.bill_candidate("bounded_wait", process_p90_ms=3.0, wait_ms=500.0)
    assert over["billed_ms"] == 503.0 and not over["under_bar"]


def test_the_drain_wait_is_charged_to_a_waiting_candidate_on_the_strict_reading():
    plain = mod.bill_candidate("bounded_wait", 3.0, 300.0)
    strict = mod.bill_candidate("bounded_wait", 3.0, 300.0, mod.DRAIN_WAIT_MS)
    assert plain["under_bar"] and not strict["under_bar"]


def test_choose_refuses_over_bar_and_contract_breaking_candidates():
    bills = [
        mod.bill_candidate("fail_fast", 30.0),
        mod.bill_candidate("slim_cold", 185.0, 2000.0),
    ]
    assert bills[0]["under_bar"] and not bills[0]["contract_ok"]
    assert not bills[1]["under_bar"]
    assert mod.choose(bills) is None


def test_the_python_boot_wait_keeps_the_baseline_and_slim_entry_over_the_bar():
    bills = mod.build_bills(_report())
    assert not bills["baseline"]["under_bar"]
    assert bills["baseline"]["billed_ms"] == 2190.0
    by_name = {b["candidate"]: b for b in bills["plain"]}
    assert not by_name["slim_cold"]["under_bar"]
    assert by_name["cold_no_wait"]["under_bar"]
    assert bills["choice"] == "cold_no_wait"
    assert bills["choice_strict_drain_reading"] == "cold_no_wait"


def test_a_bounded_wait_longer_than_the_bar_cannot_be_chosen():
    by_name = {b["candidate"]: b for b in mod.build_bills(_report(boot_wall_p90=4200.0))["plain"]}
    assert by_name["bounded_wait"]["wait_budget_ms"] == 4200.0
    assert not by_name["bounded_wait"]["under_bar"]


def test_a_short_boot_makes_the_bounded_wait_pass_plain_but_not_on_the_strict_drain_reading():
    bills = mod.build_bills(_report(boot_wall_p90=251.0))
    plain = {b["candidate"]: b for b in bills["plain"]}
    strict = {b["candidate"]: b for b in bills["strict_drain_reading"]}
    assert plain["bounded_wait"]["under_bar"]
    assert not strict["bounded_wait"]["under_bar"]


def test_every_candidate_the_bills_name_has_a_contract_verdict():
    for bill in mod.build_bills(_report())["plain"]:
        assert bill["candidate"] in mod.CONTRACT_OK


@pytest.mark.spawns_process
def test_the_pinned_base_door_source_is_the_committed_bytes_at_base(tmp_path):
    out = mod.export_door_sources(REPO_ROOT, mod.BASE_SHA, "posix", tmp_path)
    committed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{mod.BASE_SHA}:{mod.DOOR_DIR_REL}/door_posix.c"],
        capture_output=True, check=True,
    ).stdout
    assert (out / "door_posix.c").read_bytes() == committed
    assert b"RESPAWN ON MISS" in committed
    assert {p.name for p in out.iterdir()} == set(mod.SOURCE_FILES["posix"])


def test_worktree_export_copies_the_live_source(tmp_path):
    out = mod.export_door_sources(REPO_ROOT, mod.WORKTREE, "posix", tmp_path)
    assert (out / "door_core.c").read_bytes() == (REPO_ROOT / mod.DOOR_DIR_REL / "door_core.c").read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="hardlinks to a tmp file; the property is the same but POSIX-simple")
def test_swapping_the_cold_entry_never_writes_through_the_hardlink(tmp_path):
    source = tmp_path / "repo-entry.py"
    source.write_text("original\n", encoding="utf-8")
    root = tmp_path / "clone"
    (root / "coordinator" / "bin").mkdir(parents=True)
    os.link(source, root / mod.ENTRY_REL)
    engine = object.__new__(mod.DeadEngine)
    engine.root = root
    engine.entry_original = "original\n"

    engine.set_cold_entry(mod.SLIM_ENTRY)
    assert (root / mod.ENTRY_REL).read_text(encoding="utf-8") == mod.SLIM_ENTRY
    assert source.read_text(encoding="utf-8") == "original\n"

    engine.set_cold_entry(None)
    assert (root / mod.ENTRY_REL).read_text(encoding="utf-8") == "original\n"
    assert source.read_text(encoding="utf-8") == "original\n"


def test_the_boot_wait_is_read_from_the_cold_childs_own_announcement():
    class Stub(mod.DeadEngine):
        def __init__(self, stderr: str) -> None:
            self._stderr = stderr

        def reset(self) -> None:
            pass

        def door_call(self, env_extra=None, accept_rc=(0,)):
            return subprocess.CompletedProcess([], 0, stdout="session_id", stderr=self._stderr)

    waiting = "[warm-client] no warm server answered; this call is waiting up to 2s for it to start answering."
    assert Stub(waiting).boot_wait_billed_ms() == 2000.0
    assert Stub("[warm-client] the boot wait is off in this process").boot_wait_billed_ms() == 0.0


def test_a_cold_fall_through_does_not_count_as_a_warm_answer():
    engine = object.__new__(mod.DeadEngine)
    warm = subprocess.CompletedProcess([], 0, stdout="session_id", stderr="")
    cold = subprocess.CompletedProcess([], 0, stdout="session_id", stderr="[warm-client] ENGINE UNREACHABLE")
    assert engine.is_warm_answer(warm) and not engine.is_warm_answer(cold)


@pytest.mark.spawns_process
def test_stop_engine_processes_ends_a_process_rooted_at_the_clone(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(root)])
    try:
        deadline = time.monotonic() + 10
        while not mod.server_processes(root) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert mod.server_processes(root)
        mod.stop_engine_processes(root)
        assert proc.wait(timeout=10) is not None
        assert not mod.server_processes(root)
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.skipif(os.name == "nt", reason="asserts the refusal off Windows")
def test_the_windows_arm_refuses_to_run_off_windows():
    with pytest.raises(SystemExit, match="only on Windows"):
        mod.run(REPO_ROOT, "windows", mod.BASE_SHA, 1)


def test_the_pinned_base_is_a_full_sha():
    assert len(mod.BASE_SHA) == 40 and set(mod.BASE_SHA) <= set("0123456789abcdef")
