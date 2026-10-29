"""A held run-token claim makes the terminal commit reply inside the budget, naming the leg."""

from __future__ import annotations

import time

from coordinator_core import dispatch_abandon
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit

SCRIPT = "scratch/warp/run1.workflow.mjs"


def _held(monkeypatch, pending: bool):
    monkeypatch.setattr(run_token, "redeem", lambda *a, **k: False)
    monkeypatch.setattr(run_token, "claim_pending", lambda *a, **k: pending)
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.terminal_commit_reconcile.already_landed_reply",
        lambda *a, **k: None,
    )


def test_held_claim_replies_within_budget_naming_leg(monkeypatch, tmp_path):
    _held(monkeypatch, True)
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: 3600.0)
    start = time.monotonic()
    reply = terminal_commit._redeem_or_answer(tmp_path, "run1", "t" * 32, SCRIPT)
    assert time.monotonic() - start < 0.5
    assert reply["refused"] == "run-token-in-flight"
    assert reply["blocked_leg"] == terminal_commit.CLAIM_WAIT_LEG
    assert reply["committed"] is False


def test_unmatched_token_is_still_refused_run_token(monkeypatch, tmp_path):
    _held(monkeypatch, False)
    reply = terminal_commit._redeem_or_answer(tmp_path, "run1", "t" * 32, SCRIPT)
    assert reply["refused"] == "run-token"
    assert "blocked_leg" not in reply
