"""A chained warp run's own ask-phase hunks are its own work, never peer dirt to split out.

Regression: d145e6 (and four DoE runs) halted verify-overlay-unverified because `peer_split`
named the run's own ask id as the peer owner, split those hunks out of the clean-overlay
candidate, and the candidate failed collection on a half-applied change.
"""
from pathlib import Path

from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import ask_stage


def _wire(monkeypatch, owner, kin):
    monkeypatch.setattr(hp, "_unproduced", lambda root, run_id, path: ([[1, 2]], [], b"head\n"))
    monkeypatch.setattr(hp, "_peer_owner", lambda root, run_id, own_plan, path: owner)
    monkeypatch.setattr(hp, "_settled_blob", lambda root, path, head, own: "b" * 40)
    monkeypatch.setattr(ask_stage, "_chain_kin", lambda root, run_id, cands: set(cands) & kin)


def test_own_chain_owner_is_not_split(tmp_path: Path, monkeypatch):
    _wire(monkeypatch, "ask-x-d145e6", {"ask-x-d145e6"})
    assert hp.peer_split(tmp_path, "warp-x-ae7857", ["a.py"]) == ({}, {})


def test_true_peer_owner_is_still_split(tmp_path: Path, monkeypatch):
    _wire(monkeypatch, "ask-x-70d6af", set())
    split, unsettled = hp.peer_split(tmp_path, "warp-x-ae7857", ["a.py"])
    assert split["a.py"]["owner"] == "ask-x-70d6af" and not unsettled


def test_unreadable_provenance_own_chain_not_unsettled(tmp_path: Path, monkeypatch):
    _wire(monkeypatch, "ask-x-d145e6", {"ask-x-d145e6"})

    def boom(root, run_id, path):
        raise OSError("unreadable")

    monkeypatch.setattr(hp, "_unproduced", boom)
    assert hp.peer_split(tmp_path, "warp-x-ae7857", ["a.py"]) == ({}, {})
