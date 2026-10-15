"""Standing gate: `dispatch.ask_fanout` under the 500ms bar with zero spawns.

UNIT: process time (`time.process_time`) and spawn count, never wall clock (CLAUDE.md, The
brightline). A budget-sized asks array runs in-process against a tmp_path repo with a 200-turn PM log.
"""

from __future__ import annotations

import os
import subprocess
import time

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.session_baton import store

BUDGET_MS = 500.0
SID = "sid-fanout-budget"


def test_fanout_is_under_the_bar_with_zero_spawns(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    for i in range(200):
        assert PT.append_turn(f"pm turn number {i} with some words", session_id=SID)["ok"]

    spawns = []
    real_popen = subprocess.Popen

    def counting_popen(*a, **k):
        spawns.append(a)
        return real_popen(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", counting_popen)
    monkeypatch.setattr(os, "system", lambda *a, **k: spawns.append(a))

    size = contract.concurrent_run_budget()
    asks = [{"ask": f"job {i}", "label": f"j{i}", "writes": [f"d{i}"]} for i in range(size + 1)]

    start = time.process_time()
    out = mod.fanout({"asks": asks, "session_id": SID}, tmp_path)
    elapsed_ms = (time.process_time() - start) * 1000

    assert out["exit_code"] == 0, out
    assert len(out["runs"]) == size + 1
    assert not spawns, f"fanout spawned {len(spawns)} process(es)"
    assert elapsed_ms < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {elapsed_ms:.1f}ms"
