"""Standing gate: the submit hook's `evaluate` on a 14-ask independent submission under the 500ms bar.

UNIT: process time (`time.process_time`) and spawn count, never wall clock (CLAUDE.md, The
brightline). Real fan-out and real launcher write, in-process against a tmp_path repo.
"""

from __future__ import annotations

import os
import subprocess
import time

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.session_baton import store

BUDGET_MS = 500.0
SID = "sid-submit-fan-budget"
ASKS = 14


def test_a_fourteen_ask_submission_is_under_the_bar_with_no_spawn_beyond_the_fanout(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    (root / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    for i in range(200):
        assert PT.append_turn(f"pm turn number {i} with some words", session_id=SID)["ok"]

    spawns: list = []
    real_popen = subprocess.Popen

    def counting_popen(*a, **k):
        spawns.append(a)
        return real_popen(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", counting_popen)
    monkeypatch.setattr(os, "system", lambda *a, **k: spawns.append(a))

    asks = [{"ask": f"job {i}", "source": "pm", "label": f"j{i}", "writes": [f"d{i}"]} for i in range(ASKS)]
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"asks": asks}},
        "session_id": SID,
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }

    start = time.process_time()
    env = mod.evaluate(payload)
    elapsed_ms = (time.process_time() - start) * 1000

    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    assert str(hso["updatedInput"]["scriptPath"]).endswith(".launcher.workflow.mjs")
    assert not spawns, f"evaluate spawned {len(spawns)} process(es)"
    assert elapsed_ms < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {elapsed_ms:.1f}ms for {ASKS} asks"
