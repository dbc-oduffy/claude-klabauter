"""A peer-claim refusal naming the plan skips stamp_landed_rows_on_refusal; one naming other paths stamps."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import refusal_coded_stamp, terminal_commit

PLAN = "docs/plans/p.md"


def _spy(monkeypatch):
    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        return {"stamped": True}

    monkeypatch.setattr(refusal_coded_stamp, "stamp_landed_rows_on_refusal", fake)
    monkeypatch.setattr("coordinator_core.lifecycle.main_worktree_root", lambda root: root)
    return calls


def test_peer_claim_refusal_naming_the_plan_skips_the_stamp(monkeypatch):
    calls = _spy(monkeypatch)
    reply = {"refused": "peer-claim", "peer_claims": [{"path": PLAN}]}

    assert terminal_commit._stamp_on_refusal({"plan_path": PLAN}, object(), reply) == {}
    assert calls == []


def test_peer_claim_refusal_naming_other_paths_still_stamps(monkeypatch):
    calls = _spy(monkeypatch)
    reply = {"refused": "peer-claim", "peer_claims": [{"path": "other.py"}]}

    assert terminal_commit._stamp_on_refusal({"plan_path": PLAN}, object(), reply) == {"stamped": True}
    assert len(calls) == 1
