"""Structure pins for the commit_v2 minimal-land spike: what lands, which phases bracket, abandon safety.

No time figure is asserted; the spike's verdict is S2's live run.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.benchmarks import commit_v2_minimal_land_spike as spike
from coordinator_core.ops.ceremony.commit_v2_phases import PhaseLedger
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "11111111-2222-4333-8444-555555555555"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "work"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    _git(["add", "--", "a.txt", "b.txt"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "a.txt").write_text("a2\n", encoding="utf-8")
    (root / "b.txt").write_text("b2\n", encoding="utf-8")
    return root


def test_two_path_land_carries_session_trailer_and_exact_phases(repo):
    ledger = PhaseLedger()
    res = spike.minimal_land(
        repo, ["a.txt", "b.txt"], "edit both\n", ledger=ledger, session_id=SID
    )
    assert "error" not in res, res
    assert _git(["rev-parse", "HEAD"], repo) == res["sha"]
    assert f"Session-Id: {SID}" in _git(["log", "-1", "--format=%B"], repo)
    assert _git(["show", "--name-only", "--format=", "HEAD"], repo).split() == ["a.txt", "b.txt"]
    assert list(res["phases_ms"]) == list(spike.KEPT_PHASES)
    assert set(res["gate_ms"]) >= {"declared_deletion_gate", "attribution_gate"}
    assert not (repo / ".git" / "index.lock").exists()


def test_abandoned_dispatch_moves_no_ref_and_leaves_no_lock(repo):
    head = _git(["rev-parse", "HEAD"], repo)
    ev = threading.Event()
    ev.set()
    token = dispatch_abandon.ACTIVE.set(ev)
    try:
        res = spike.minimal_land(
            repo, ["a.txt", "b.txt"], "edit both\n", ledger=PhaseLedger(), session_id=SID
        )
    finally:
        dispatch_abandon.ACTIVE.reset(token)
    assert "sha" not in res and res["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert not (repo / ".git" / "index.lock").exists()


def test_refusals_return_an_error_and_land_nothing(repo):
    head = _git(["rev-parse", "HEAD"], repo)
    for paths, message in (([], "m\n"), (["a.txt"], "  \n")):
        res = spike.minimal_land(repo, paths, message, ledger=PhaseLedger(), session_id=SID)
        assert "error" in res and "sha" not in res
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_reserved_lines_lower_the_threshold():
    assert spike.threshold_ms() == spike.BAR_MS - sum(spike.RESERVED_MS.values())
    assert set(spike.RESERVED_MS) <= set(spike.BUDGET_MS)


def _sample(process_ms, child_ms, **phases):
    return {"sha": "x", "process_ms": process_ms, "child_inclusive_ms": child_ms, "phases_ms": phases}


def test_judge_gates_on_both_clocks_and_judges_warm_samples_only():
    cold = _sample(9999.0, 9999.0, land=1.0)
    ok = _sample(100.0, 120.0)
    assert spike.judge([cold, ok, ok]) == "SPIKE: AT-BAR"
    assert spike.judge([cold, _sample(100.0, 900.0), ok]).startswith("SPIKE: OVER-BAR -- child_inclusive_ms")
    assert spike.judge([cold, _sample(900.0, 100.0), ok]).startswith("SPIKE: OVER-BAR -- process_ms")
    over_phase = _sample(100.0, 120.0, claim_lookup=300.0)
    assert spike.judge([cold, over_phase, over_phase], 15.625) == "SPIKE: OVER-BAR -- claim_lookup 300.0"
    assert spike.judge([cold]).startswith("SPIKE: INSUFFICIENT")
    refused = {"error": "x", "process_ms": 1.0, "child_inclusive_ms": 1.0, "phases_ms": {}}
    assert spike.judge([cold, ok, refused]) == "SPIKE: INSUFFICIENT -- 1 of 2 warm samples refused"


TICK = 15.625


def test_phase_at_one_tick_under_a_tick_floored_budget_is_not_over_bar():
    cold = _sample(9999.0, 9999.0)
    one_tick = _sample(100.0, 120.0, apply_missing_trailers=15.6)
    assert spike.judge([cold, one_tick, one_tick], TICK) == "SPIKE: AT-BAR"


def test_phase_within_one_tick_over_effective_budget_is_watch_not_over_bar():
    warm = [_sample(100.0, 120.0, apply_missing_trailers=30.0)]
    over, watch = spike.phase_findings(warm, TICK)
    assert over == [] and watch == [("apply_missing_trailers", 30.0)]
    cold = {**_sample(9999.0, 9999.0), "spawns": 0}
    assert spike.judge([cold, warm[0], warm[0]], TICK) == "SPIKE: AT-BAR"
    lines = list(spike._summary([cold, warm[0], warm[0]], TICK))
    assert "PHASE-WATCH -- apply_missing_trailers 30.0" in lines
    assert f"TICK_MS: {TICK:.3f}" in lines


def test_phase_more_than_one_tick_over_is_over_bar():
    cold = _sample(9999.0, 9999.0)
    far = _sample(100.0, 120.0, apply_missing_trailers=46.9)
    assert spike.judge([cold, far, far], TICK) == "SPIKE: OVER-BAR -- apply_missing_trailers 46.9"


def test_phase_median_sum_over_threshold_is_over_bar():
    cold = _sample(9999.0, 9999.0)
    total = _sample(100.0, 120.0, claim_lookup=110.0, pre_commit_gates=50.0, **{"land.product": 291.0})
    assert sum(total["phases_ms"].values()) == 451.0
    verdict = spike.judge([cold, total, total], TICK)
    assert verdict.startswith("SPIKE: OVER-BAR -- phase median sum 451.0")


def test_measure_tick_is_positive_and_measured():
    assert spike.measure_tick_ms(1) > 0
