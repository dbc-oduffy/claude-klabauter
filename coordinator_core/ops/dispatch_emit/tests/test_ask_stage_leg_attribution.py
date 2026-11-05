"""dispatch.ask_stage process_time rows carry per-leg attribution, driven through dispatch_message."""

from __future__ import annotations

import asyncio
import json
import time

import coordinator_core.ipc as ipc
from coordinator_core.bash_guards import leg_attribution
from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests._size_l_plan import build_size_l_plan, isolate_launch_dirs

_OP = "dispatch.ask_stage"


def _burn(ms: float) -> None:
    end = time.thread_time() + ms / 1000.0
    while time.thread_time() < end:
        pass


def _call(repo, monkeypatch, params) -> tuple[dict, list[dict]]:
    common = repo / ".git"
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common)
    monkeypatch.setattr(ask_stage, "main_worktree_root", lambda p: repo)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)
    msg = {"jsonrpc": "2.0", "id": 1, "method": _OP, "params": params, "_origin_worktree": str(repo)}
    response = asyncio.run(ipc.dispatch_message(msg))
    assert "result" in response, response
    sink = common / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    return response["result"], [r for r in rows if r.get("kind") == "process_time" and r.get("op") == _OP]


def _fixture(tmp_path, monkeypatch):
    isolate_launch_dirs(monkeypatch, tmp_path)
    repo, plan_rel, _ = build_size_l_plan(tmp_path, rows=2, live_tokens=0)
    run_id = "leg-attribution-run"
    # A launcher run manifest is refused unless its run script exists; one with no inline slot needs no twin.
    script = repo / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("// run script\n", encoding="utf-8", newline="\n")
    return repo, {"run_id": run_id, "plan_path": plan_rel}


def test_forced_slow_stage_phase_is_named_in_the_row_through_to_thread(tmp_path, monkeypatch):
    repo, params = _fixture(tmp_path, monkeypatch)
    real = ask_stage._stage_write_baseline

    def slow(*a, **k):
        _burn(300.0)
        return real(*a, **k)

    monkeypatch.setattr(ask_stage, "_stage_write_baseline", slow)
    result, rows = _call(repo, monkeypatch, params)
    assert "manifest_handle" in result
    assert len(rows) == 1
    row = rows[0]
    assert row["measurement_scope"] == ipc.MEASUREMENT_SCOPE_PER_OP_HANDLER
    by_name = {leg["name"]: leg["ms"] for leg in row["legs"]}
    assert by_name.get("baseline", 0.0) >= 250.0
    assert row["handler_thread_ms"] >= by_name["baseline"]
    assert len(row["legs"]) <= 3


def test_forced_slow_handler_phase_is_named_outside_stage(tmp_path, monkeypatch):
    repo, params = _fixture(tmp_path, monkeypatch)
    real = ask_stage.stamp_plan_executing

    def slow(*a, **k):
        _burn(300.0)
        return real(*a, **k)

    monkeypatch.setattr(ask_stage, "stamp_plan_executing", slow)
    _, rows = _call(repo, monkeypatch, params)
    by_name = {leg["name"]: leg["ms"] for leg in rows[0]["legs"]}
    assert by_name.get("stamp", 0.0) >= 250.0


def test_early_return_carries_handler_thread_ms_and_drains_the_collector(tmp_path, monkeypatch):
    repo, params = _fixture(tmp_path, monkeypatch)
    _call(repo, monkeypatch, params)
    again, rows = _call(repo, monkeypatch, params)
    assert again.get(ask_stage.ask_stage_receipt.ALREADY_STAGED_KEY) is True
    assert "handler_thread_ms" in rows[-1]
    assert not leg_attribution.active()


def test_error_reply_drains_the_collector_and_still_attaches(tmp_path, monkeypatch):
    repo, params = _fixture(tmp_path, monkeypatch)
    result, rows = _call(repo, monkeypatch, {**params, "plan_path": "docs/plans/absent.md"})
    assert "error" in result
    assert "handler_thread_ms" in rows[0]
    assert not leg_attribution.active()
