"""A warm-served default dispatch.terminal_commit whose kept-blob chain overruns its deadline reads `finished`, never `executing` or `unknowable`."""

from __future__ import annotations

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.ops.dispatch_emit import land_deadline, terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    _REVIEWED,
    _SID,
    _git,
    repo,
)
from coordinator_core.telemetry import op_latency
from coordinator_core.warm import dispatch_ack, server

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP = "ceremony.fake_warm_terminal_commit"
_LEG = "peer_hunk_gate.co_declared_peer_holds"


@pytest.fixture
def warm_env(monkeypatch, repo):
    monkeypatch.setenv(op_latency.ROUTE_ENV, op_latency.WARM_SERVER)
    import coordinator_core.warm.client as wc

    monkeypatch.setattr(wc, "_op_may_mutate", lambda m: True)
    monkeypatch.setattr(ipc, "_report_ceremony_overrun", lambda *a: None)
    monkeypatch.setattr(server, "_ack_store", dispatch_ack.AckStore())
    monkeypatch.setitem(
        ipc._REGISTRY,
        _OP,
        lambda params, ctx=None, repo_root=None: terminal_commit._handler(params, repo_root=repo / ".git"),
    )
    return repo


def _msg(params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 7, "method": _OP, "params": params}


def _params() -> dict:
    return {
        "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, "session_id": _SID,
        "ledger_verdict": {"items": [{"id": "R1", "status": "met", "wired": True}]},
    }


def _overrun(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(land_deadline, "_now", lambda: clock[0])

    def overrun(*args, **kwargs):
        clock[0] += land_deadline.LAND_BUDGET_SECS + land_deadline.RESERVE_SECS + 1.0
        land_deadline.check(_LEG)
        raise AssertionError("check must raise past the deadline")

    monkeypatch.setattr(terminal_commit, "_landing_integrity_gate", overrun)


def _poll(key: str) -> dict:
    return server._ack_store.status(key)


def test_an_overrunning_terminal_commit_polls_finished_with_the_named_halt(warm_env, monkeypatch):
    _overrun(monkeypatch)
    head = _git(warm_env, "rev-parse", "HEAD")
    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, _OP)
    assert _poll(key)["state"] == dispatch_ack.STATE_EXECUTING

    response = server._run_dispatch(_msg(_params()), dispatch_key=key)

    status = _poll(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED, status
    # The halt is the op's structured refusal, so the record carries it as a `result`, not a transport `error`.
    assert status["outcome"] == dispatch_ack.OUTCOME_RESULT, status
    assert status["result"]["refused"] == land_deadline.REFUSED
    assert status["result"]["committed"] is False
    body = response.get("result") or {}
    assert body.get("refused") == land_deadline.REFUSED, response
    assert body.get("leg") == _LEG
    assert _git(warm_env, "rev-parse", "HEAD") == head
    assert _git(warm_env, "diff", "--cached", "--name-only").strip() == ""


def test_a_terminal_commit_inside_its_budget_polls_finished_with_the_landing(warm_env):
    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, _OP)

    server._run_dispatch(_msg(_params()), dispatch_key=key)

    status = _poll(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED, status
    assert status["outcome"] == dispatch_ack.OUTCOME_RESULT
    assert status["result"]["committed"] is True, status["result"]
