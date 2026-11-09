"""A multi-ask submission that outruns the hook budget after minting only some entries names the unminted ones
with their re-submit args; a fully minted or nothing-minted overrun carries no such tail."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real

pytestmark = pytest.mark.complete

repo = real.repo
ASKS = [{"ask": f"do thing number {i}", "source": "relayed", "label": f"t{i}"} for i in range(5)]


def _reason(env: dict) -> str:
    hso = env["hookSpecificOutput"]
    return hso.get("permissionDecisionReason", "") + hso.get("additionalContext", "")


def _submit(root: Path, monkeypatch, minted_count: int, asks=None) -> dict:
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    hold = threading.Event()

    def launch(root_, parent_id, children, session_id):
        for child in list(children)[:minted_count]:
            mod._note_minted(child.run_id)
        hold.wait(30)
        raise RuntimeError("released")

    try:
        return mod.evaluate(real._payload(root, {"asks": asks or ASKS}), launch=launch)
    finally:
        hold.set()


def test_partial_mint_overrun_names_each_unminted_entry_with_its_resubmit_args(repo, monkeypatch):
    env = _submit(repo, monkeypatch, 3)
    reason = _reason(env)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert "minted 3 of 5 entries" in reason
    for i in (3, 4):
        args = json.dumps({"asks": [ASKS[i]]})
        assert f"asks[{i}] (t{i}) was NOT minted; re-submit args: {args}" in reason
    for i in (0, 1, 2):
        assert f"asks[{i}]" not in reason


def test_entry_held_on_a_predecessor_is_not_named_unminted(repo, monkeypatch):
    asks = [dict(a) for a in ASKS[:4]]
    asks[3]["after"] = ["t0"]
    reason = _reason(_submit(repo, monkeypatch, 2, asks))
    assert "asks[2] (t2) was NOT minted" in reason
    assert "asks[3]" not in reason


def test_fully_minted_overrun_names_no_unminted_entry(repo, monkeypatch):
    reason = _reason(_submit(repo, monkeypatch, 5))
    assert "NOT minted" not in reason and "entries" not in reason


def test_nothing_minted_overrun_names_no_unminted_entry(repo, monkeypatch):
    reason = _reason(_submit(repo, monkeypatch, 0))
    assert "NOT minted" not in reason


def test_unminted_tail_deny_fallback_text():
    call = {"entries": [
        {"index": 0, "label": "a", "run_id": "r0", "item": {"ask": "x"}},
        {"index": 1, "label": "b", "run_id": "r1", "item": {"ask": "y"}},
    ]}
    tail = mod._unminted_tail(call, ["r0"])
    assert 'asks[1] (b) was NOT minted; re-submit args: {"asks": [{"ask": "y"}]}' in tail
    denied = mod._minted_start_envelope(None, ["r0"], 0.0, call)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "asks[1] (b) was NOT minted" in _reason(denied)
    assert mod._unminted_tail(call, ["r0", "r1"]) == "" and mod._unminted_tail(call, []) == ""


def test_duplicate_label_is_not_attributed_to_the_first_entry():
    asks = [{"label": "a"}, {"label": "a"}, {"label": "b"}]
    ready = [type("R", (), {"label": lbl, "run_id": rid})() for lbl, rid in (("a", "r1"), ("b", "r3"))]
    call: dict = {}
    mod._note_entries(call, asks, ready)
    assert [e["label"] for e in call["entries"]] == ["b"]
