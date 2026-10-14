"""Standing gate: `sizing.intake_mint` under the 500ms bar with zero spawns.

UNIT: process time (`time.process_time`) and spawn count, never wall clock (CLAUDE.md, The
brightline). The mint runs in-process against a tmp_path repo with a 200-turn PM log.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops import sizing_intake as mod
from coordinator_core.session_baton import store

BUDGET_MS = 500.0
SID = "sid-budget"


def test_mint_is_under_the_bar_with_zero_spawns(tmp_path, monkeypatch):
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

    start = time.process_time()
    out = mod._handler({"name": "budget", "session_id": SID, "em_brief": "b"}, repo_root=tmp_path)
    elapsed_ms = (time.process_time() - start) * 1000

    assert out["exit_code"] == 0, out
    assert not spawns, f"mint spawned {len(spawns)} process(es)"
    assert elapsed_ms < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {elapsed_ms:.1f}ms"
