"""land_run auto-land: a reviewed-green halted run whose declared paths only halted runs withhold is released and recorded landed."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed, land_run, peer_hunk_gate
from coordinator_core.ops.dispatch_emit.tests import test_halted_hold_release as fx

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _armed(tmp_path, *, me_marker="halted"):
    root = fx._root(tmp_path)
    me, peer = fx._auto_land_fixture(root)
    if me_marker:
        fx._write(root / "state/warp" / me / "halted.json", json.dumps({"status": me_marker, "sizing_rel": "state/sizings/other.yaml"}))
    return root, me, peer


def _no_siblings(monkeypatch, holds=()):
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: list(holds))


def _release(root, me, peer):
    return land_run._halted_only_release(root, me, None, {"a.py": peer}, ["a.py"], None)


def test_halted_only_holders_release_the_landing(tmp_path, monkeypatch):
    root, me, peer = _armed(tmp_path)
    _no_siblings(monkeypatch, [("a.py", peer)])
    assert _release(root, me, peer) == [peer]


def test_live_peer_holder_still_refuses(tmp_path, monkeypatch):
    root, me, peer = _armed(tmp_path)
    live = fx._rid("ask", 200, "cccccc")
    fx._run(root, live, writes=("a.py",))
    fx._index(root, live, "a.py")
    _no_siblings(monkeypatch, [("a.py", live)])
    assert _release(root, me, peer) is None


def test_live_co_declaring_sibling_refuses_even_when_withholders_are_halted(tmp_path, monkeypatch):
    root, me, peer = _armed(tmp_path)
    _no_siblings(monkeypatch, [("a.py", "ask-live-sibling")])
    assert _release(root, me, peer) is None
    _no_siblings(monkeypatch, [("a.py", peer_hunk_gate.UNREADABLE_PEER_CLAIMS)])
    assert _release(root, me, peer) is None


def test_red_review_refuses(tmp_path, monkeypatch):
    root, me, peer = _armed(tmp_path)
    fx._green(root, me, incomplete=["X1"])
    _no_siblings(monkeypatch, [("a.py", peer)])
    assert _release(root, me, peer) is None


def test_run_without_halted_marker_keeps_the_refusal(tmp_path, monkeypatch):
    root, me, peer = _armed(tmp_path, me_marker=None)
    _no_siblings(monkeypatch, [("a.py", peer)])
    assert _release(root, me, peer) is None


def test_unreadable_halt_record_holder_keeps_the_refusal(tmp_path, monkeypatch):
    root, me, peer = _armed(tmp_path)
    _no_siblings(monkeypatch)
    assert land_run._halted_only_release(root, me, None, {"a.py": land_run.UNREADABLE_HALT_RECORD}, ["a.py"], None) is None
    assert land_run._halted_only_release(root, me, None, {}, ["a.py"], None) is None


def test_record_goes_through_the_op_handler_with_the_landed_sha(tmp_path, monkeypatch):
    seen = {}

    def handler(params, repo_root=None):
        seen.update(params)
        return {"ok": True}

    monkeypatch.setattr(halted_run_landed, "_handler", handler)
    out = land_run._record_halted_auto_landed(tmp_path, "ask-1", "abc1234", "")
    assert out == {"run_id": "ask-1", "ok": True}
    assert seen == {"run_id": "ask-1", "sha": "abc1234"}


def test_record_failure_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(halted_run_landed, "_handler", lambda p, r=None: {"ok": False, "error": "nope"})
    assert land_run._record_halted_auto_landed(tmp_path, "ask-1", "abc1234", None) == {
        "run_id": "ask-1", "ok": False, "error": "nope",
    }

    def boom(p, r=None):
        raise RuntimeError("x")

    monkeypatch.setattr(halted_run_landed, "_handler", boom)
    assert land_run._record_halted_auto_landed(tmp_path, "ask-1", "abc1234", None)["ok"] is False
