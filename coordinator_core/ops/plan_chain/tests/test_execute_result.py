"""execute_result classifies each execute-digest branch and the commit reply."""
from __future__ import annotations

import pytest

from coordinator_core.ops.plan_chain import execute_result as er
from coordinator_core.ops.plan_chain.contract import Halt, WorkflowResult

PARAMS = {"incomplete_chunks": [], "inline_review": None, "script_path": "s.mjs"}


def _digest(**over):
    d = {
        "outcome": "completed",
        "halted": None,
        "deviations": [],
        "review": {"status": "integrated", "delivery": {"verdict": "PASS"}},
        "criterion": {"status": "met", "observation": None, "sidecar": None},
        "next_action": {"kind": "terminal_commit", "op": "dispatch.terminal_commit", "params": PARAMS},
    }
    d.update(over)
    return d


def _wr(digest):
    return WorkflowResult(digest=digest, raw_result="", child_session_id="sid")


def test_clean_digest_returns_params_unmodified():
    out = er.read_execute_result(_wr(_digest()))
    assert out is PARAMS


@pytest.mark.parametrize(
    "digest, stage",
    [
        (None, "execute"),
        (_digest(outcome="halted", halted="usage_limit: resets 5pm"), "execute"),
        (_digest(outcome="halted", halted="C3"), "execute"),
        (_digest(deviations=[{"chunk": "C2", "kind": "blocked", "anchor": "a"}]), "execute"),
        (_digest(review={"status": "integrated", "delivery": {"verdict": "FAIL"}}), "review"),
        (_digest(review={"status": "unstructured", "delivery": {"verdict": "unstructured"}}), "review"),
        (_digest(criterion={"status": "not_met", "observation": "o", "sidecar": None}), "review"),
        (_digest(next_action={"kind": "none", "op": None, "params": None}), "execute"),
    ],
)
def test_halt_branches_by_stage(digest, stage):
    out = er.read_execute_result(_wr(digest))
    assert isinstance(out, Halt) and out.halted_at == stage


def test_halt_reasons_distinguish_usage_limit_and_block():
    assert "usage_limit" in er.read_execute_result(
        _wr(_digest(outcome="halted", halted="usage_limit"))
    ).reason
    out = er.read_execute_result(_wr(_digest(deviations=[{"chunk": "C2", "kind": "blocked", "anchor": "x"}])))
    assert "C2" in out.reason


def test_partial_deviation_is_not_a_block():
    d = _digest(deviations=[{"chunk": "C2", "kind": "partial", "anchor": "x"}])
    assert er.read_execute_result(_wr(d)) is PARAMS


def test_commit_reply_ok_and_refusals():
    ok = er.read_commit_reply({"committed": True, "sha": "abc", "receipts": ["r/1.json"]})
    assert ok == {"sha": "abc", "receipt_path": "r/1.json"}
    assert er.read_commit_reply({"result": {"committed": True, "sha": "abc"}}) == {
        "sha": "abc",
        "receipt_path": "",
    }
    for bad in (None, {"committed": False, "sha": None, "error": "dirty"}, {"error": {"message": "no"}}):
        h = er.read_commit_reply(bad)
        assert isinstance(h, Halt) and h.halted_at == "terminal-commit"
    assert "dirty" in er.read_commit_reply({"committed": False, "sha": None, "error": "dirty"}).reason


def test_stages_seen():
    assert er.stages_seen(_digest()) == ["execute", "review"]
    assert er.stages_seen(_digest(review={"status": "not_run"})) == ["execute"]
    assert er.stages_seen(None) == []
