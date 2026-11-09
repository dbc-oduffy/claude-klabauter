"""hooks.preuse_bash_dispatch rows carry per-leg attribution, driven through dispatch_message."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.bash_guards import dispatch as chain
from coordinator_core.hooks import preuse_bash_dispatch  # noqa: F401  (registers the op)

_OP = "hooks.preuse_bash_dispatch"


def _burn(ms: float) -> None:
    end = time.thread_time() + ms / 1000.0
    while time.thread_time() < end:
        pass


def _call(tmp_path, monkeypatch, command="ls"):
    common = tmp_path / ".git"
    common.mkdir()
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": _OP,
        "params": {"payload": {"tool_name": "Bash", "tool_input": {"command": command},
                               "session_id": "s", "cwd": str(tmp_path)}},
        "_origin_worktree": str(tmp_path),
    }
    response = asyncio.run(ipc.dispatch_message(msg))
    assert "result" in response
    sink = common / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r.get("kind") == "process_time" and r.get("op") == _OP]


def _one_guard(fn):
    entry = chain.GuardEntry(name="slow-guard", fn=fn, fail_closed=False, band=list(chain.GuardBand)[0])
    return lambda *a, **k: [entry]


def test_forced_slow_guard_is_named_in_the_row_through_to_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(chain, "_build_guard_chain", _one_guard(lambda: (_burn(80.0), None)[1]))
    rows = _call(tmp_path, monkeypatch)
    assert len(rows) == 1
    row = rows[0]
    assert row["measurement_scope"] == ipc.MEASUREMENT_SCOPE_PER_OP_HANDLER
    assert [leg["name"] for leg in row["legs"]][0] == "slow-guard"
    assert row["legs"][0]["ms"] >= 50.0
    assert row["handler_thread_ms"] >= row["legs"][0]["ms"]


def test_fast_call_carries_handler_thread_ms_and_no_legs(tmp_path, monkeypatch):
    monkeypatch.setattr(chain, "_build_guard_chain", _one_guard(lambda: None))
    rows = _call(tmp_path, monkeypatch)
    assert len(rows) == 1
    assert "legs" not in rows[0]
    assert rows[0]["handler_thread_ms"] >= 0.0


def test_slow_claims_phase_is_named(tmp_path, monkeypatch):
    monkeypatch.setattr(chain, "_build_guard_chain", _one_guard(lambda: None))
    monkeypatch.setattr(chain, "_record_bash_write_claims", lambda raw, result: _burn(80.0))
    rows = _call(tmp_path, monkeypatch)
    assert rows[0]["legs"][0]["name"] == "claims"


def test_slow_guard_stops_later_guards_at_the_hook_deadline_and_is_named(tmp_path, monkeypatch):
    from coordinator_core.hooks import _preuse_phases as pp

    ran: list[str] = []

    def slow():
        ran.append("slow-guard")
        time.sleep(0.2)

    def later():
        ran.append("later-guard")

    band = list(chain.GuardBand)[0]
    entries = [
        chain.GuardEntry(name="slow-guard", fn=slow, fail_closed=False, band=band),
        chain.GuardEntry(name="later-guard", fn=later, fail_closed=False, band=band),
    ]
    monkeypatch.setattr(chain, "_build_guard_chain", lambda *a, **k: entries)
    monkeypatch.setattr(pp, "_door_deadline_s", lambda: 2.1)
    rows = _call(tmp_path, monkeypatch)
    assert ran == ["slow-guard"]
    row = rows[0]
    assert row["slow_phase"] == "slow-guard"
    assert row["deadline_stop"] == "later-guard"
    assert row["spans"][0]["wall_ms"] >= 150


def test_guard_within_the_deadline_is_not_stopped(tmp_path, monkeypatch):
    from coordinator_core.hooks import _preuse_phases as pp

    ran: list[str] = []
    band = list(chain.GuardBand)[0]
    entries = [
        chain.GuardEntry(name="first", fn=lambda: ran.append("first"), fail_closed=False, band=band),
        chain.GuardEntry(name="second", fn=lambda: ran.append("second"), fail_closed=False, band=band),
    ]
    monkeypatch.setattr(chain, "_build_guard_chain", lambda *a, **k: entries)
    monkeypatch.setattr(pp, "_door_deadline_s", lambda: 60.0)
    rows = _call(tmp_path, monkeypatch)
    assert ran == ["first", "second"]
    assert "deadline_stop" not in rows[0]


def test_other_ops_rows_carry_no_attribution_fields(tmp_path, monkeypatch):
    async def handler(params, repo_root=None):
        return {}

    monkeypatch.setitem(ipc._REGISTRY, "test.no_attribution", handler)
    common = tmp_path / ".git"
    common.mkdir()
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)
    asyncio.run(ipc.dispatch_message({"jsonrpc": "2.0", "id": 1, "method": "test.no_attribution",
                                      "params": {}, "_origin_worktree": str(tmp_path)}))
    sink = common / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    rows = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines()]
    pt = [r for r in rows if r.get("kind") == "process_time"]
    assert pt and all("legs" not in r and "handler_thread_ms" not in r for r in pt)
