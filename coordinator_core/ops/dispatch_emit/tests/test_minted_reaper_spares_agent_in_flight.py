"""A planning run (Workflow started) whose agent is still writing its session workflow journal is not
abandoned at `minted`: the plan-author agent writes no run-dir file for tens of minutes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit.tests.test_minted_reaper_live_run_timelines import (  # noqa: F401
    ASK, MIN, PEER, SIZING, _halted_kind, _live, _make_run, repo,
)
from coordinator_core.usage import run_locate


def _session_dir(tmp: Path, journal_age: float) -> Path:
    wf = tmp / "sess" / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    j = wf / "journal.jsonl"
    j.write_text("{}\n", encoding="utf-8")
    when = time.time() - journal_age
    os.utime(j, (when, when))
    return tmp / "sess"


def _planning_run(repo):
    d = _make_run(repo, ASK, stage="minted", age=60 * MIN, plan_age=50 * MIN, run_json={"sizing_rel": SIZING}, held=True)
    stage = json.loads((d / "run-stage.json").read_text(encoding="utf-8"))
    stage["session_id"] = PEER
    (d / "run-stage.json").write_text(json.dumps(stage), encoding="utf-8")
    old = time.time() - 60 * MIN
    os.utime(d / "run-stage.json", (old, old))
    return d


def test_planning_run_with_a_live_agent_journal_is_spared(repo, tmp_path, monkeypatch):
    ask = _planning_run(repo)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: _session_dir(tmp_path, 2 * MIN))
    assert _live(repo, ASK)
    assert _halted_kind(ask) is None


def test_planning_run_with_a_quiet_journal_is_still_abandoned(repo, tmp_path, monkeypatch):
    ask = _planning_run(repo)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: _session_dir(tmp_path, 40 * MIN))
    assert not _live(repo, ASK)
    assert _halted_kind(ask) == wrd.ABANDONED_AT_MINTED

