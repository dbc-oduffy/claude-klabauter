"""A gate call whose bind and plan stamp landed after the client deadline reads back as committed on re-run."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.ops.dispatch_emit.ask_contract import PLAN_STARTED_FIELD, record_dir, write_run_stage
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put
from coordinator_core.telemetry import op_latency

RUN = "warp-2026-11-23-late-commit-20261123T070000-aaaaaa"


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    _put(tmp_path, tshirt="S", route="spec-dispatch")
    return tmp_path


def _stamp(repo: Path) -> str:
    stage = json.loads((record_dir(repo, RUN) / "run-stage.json").read_text(encoding="utf-8"))
    return stage[PLAN_STARTED_FIELD]


def test_rerun_after_late_commit_returns_the_bound_verdict_without_a_second_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    write_run_stage(repo, RUN, "minted")
    params = {"sizing_path": REL, "run_id": RUN}

    first = _handler(dict(params), repo_root=repo)  # committed; the caller never saw this reply
    assert "error" not in first and first["sizing_path"] == REL, first
    stamped = _stamp(repo)
    time.sleep(0.02)

    second = _handler(dict(params), repo_root=repo)

    assert second == first
    assert _stamp(repo) == stamped


def test_a_hung_gate_envelope_says_the_rerun_is_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def hung(params, ctx=None, repo_root=None) -> dict:
        time.sleep(0.45)
        return {"ok": True}

    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "0.1")
    monkeypatch.setenv(op_latency.ROUTE_ENV, op_latency.IN_PROCESS)
    monkeypatch.setitem(ipc._REGISTRY, "dispatch.ask_gate", hung)
    monkeypatch.setattr(ipc, "_timeout_for", lambda m, msg=None: 0.1)
    monkeypatch.setattr(ipc, "git_common_dir", lambda *a, **k: tmp_path / ".git")
    msg = {"jsonrpc": "2.0", "id": 1, "method": "dispatch.ask_gate", "params": {}, "_origin_worktree": str(tmp_path)}

    env = asyncio.run(ipc._dispatch_message_impl(msg))

    data = env["error"]["data"]
    assert env["error"]["code"] == -32004
    assert data["retry_safe"] is True
    assert "Retry is NOT safe" not in env["error"]["message"]


def test_other_cold_mutating_ops_stay_unsafe_to_retry() -> None:
    env = ipc._hung_past_ceiling_envelope("ceremony.fake", 1.0, 5.0, 2.0, 1)

    assert env["error"]["data"]["retry_safe"] is False
    assert "Retry is NOT safe" in env["error"]["message"]
