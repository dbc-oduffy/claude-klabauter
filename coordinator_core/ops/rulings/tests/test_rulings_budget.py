"""Budget falsifier (DR-344): each rulings.* op, dispatched through coordinator_core.ipc over
a 200-record register, spawns no subprocess and costs under 200ms of process time.

Set ``RULINGS_BUDGET_REPORT=1`` to print the per-op figures for the plan evidence line.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time

import pytest

from coordinator_core import ipc
from coordinator_core.ops.rulings import challenge, coverage, gate, index, search  # noqa: F401  (register ops)
from coordinator_core.rulings import store
from coordinator_core.rulings.contract import Ruling

BUDGET_S = 0.200
N_RECORDS = 200

_CALLS = {
    "rulings.index": {"verb": "list"},
    "rulings.search": {"scope": ["plan.prep_gate"], "query": "private"},
    "rulings.gate": {"body": "plan.prep_gate scope is pending a PM ruling on thresholds?"},
    "rulings.coverage": {},
    "deferral.challenge": {
        "surface": "reviewer_unapplied",
        "text": "Not now; follow-up later.",
        "fields": {"unapplied_reason": "not now"},
        "session_id": "budget-probe",
    },
}


@pytest.fixture
def register(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    for i in range(N_RECORDS):
        reply = store.append(
            tmp_path,
            Ruling(
                id=f"rl-{i:03d}",
                pm_turn="state/sizings/x.yaml R2",
                scope=("plan.prep_gate", f"area-{i}"),
                text=f"This is a private repo; ruling number {i} is settled.",
                enforcement="coordinator_core/rulings/store.py" if i % 2 else None,
                recorded="2026-11-21",
            ),
        )
        assert reply["applied"] == [f"rl-{i:03d}"], reply
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)

    spawned: list = []

    def boom(*a, **k):
        spawned.append(a)
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    return tmp_path, spawned


def _call(root, op, params):
    msg = {"jsonrpc": "2.0", "id": 1, "method": op, "params": params, "_origin_worktree": str(root)}
    return asyncio.run(ipc.dispatch_message(msg))


@pytest.mark.parametrize("op", sorted(_CALLS))
def test_op_is_spawn_free_and_under_budget(register, op):
    root, spawned = register
    reply = _call(root, op, _CALLS[op])
    assert "error" not in reply, reply

    start = time.process_time()
    reply = _call(root, op, _CALLS[op])
    elapsed = time.process_time() - start

    assert "error" not in reply, reply
    assert spawned == []
    if os.environ.get("RULINGS_BUDGET_REPORT"):
        print(f"RULINGS_BUDGET {op} process_ms={elapsed * 1000:.1f} spawns={len(spawned)}")
    assert elapsed < BUDGET_S, f"{op}: {elapsed * 1000:.1f}ms process time over {BUDGET_S * 1000:.0f}ms"
