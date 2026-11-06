"""review.prep reviews a path the run itself wrote even when a live peer has touched it, and names the peer touch.

Review is read-only: a peer's touch never hides the run's own hunks from the reviewers. The peer-held
paths ride on the prep reply's ``peer_held``; the landing-side filter keeps its path-level contract.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.session import claim_index, liveness

RUN = "ask-20261106T002934-26c3fc"
PROD = "pkg/mod.py"
TEST = "pkg/tests/test_mod.py"
PEER = "456dea75"
BASE = "a" * 40


class _Found(dict):
    recorded_kind = None


def _stage(tmp_path, declared):
    base = record_dir(tmp_path, RUN)
    base.mkdir(parents=True, exist_ok=True)
    (base / "manifest.json").write_text(json.dumps({"review_declared_paths": declared}), encoding="utf-8")


def _peer_touches(monkeypatch, paths):
    def lookup(asked, **kw):
        found = _Found({p: [PEER] if p in paths else [] for p in asked})
        found.recorded_kind = {p: {PEER: "write"} if p in paths else {} for p in asked}
        return found

    monkeypatch.setattr(claim_index, "lookup", lookup)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd=None: False)
    monkeypatch.setattr(warp_run_deliverable, "live_run_write_claims", lambda *a, **kw: {})


def test_a_declared_path_a_peer_also_touched_stays_reviewable(tmp_path, monkeypatch):
    _stage(tmp_path, [PROD, TEST])
    _peer_touches(monkeypatch, {TEST})

    paths, held = rp.declared_paths_and_held(tmp_path, RUN, own_session="mine")

    assert paths == [PROD, TEST]
    assert held == {TEST: [PEER]}


def test_a_run_whose_every_path_is_peer_touched_still_reviews_them_all(tmp_path, monkeypatch):
    _stage(tmp_path, [PROD, TEST])
    _peer_touches(monkeypatch, {PROD, TEST})

    assert rp.declared_paths_and_held(tmp_path, RUN, own_session="mine")[0] == [PROD, TEST]


def test_the_landing_side_view_still_withholds_the_peer_touched_path(tmp_path, monkeypatch):
    _stage(tmp_path, [PROD, TEST])
    _peer_touches(monkeypatch, {TEST})

    assert rp.declared_paths_for_run(tmp_path, RUN, own_session="mine") == [PROD]


def test_the_prep_reply_names_the_peer_touched_path(tmp_path, monkeypatch):
    _stage(tmp_path, [PROD, TEST])
    _peer_touches(monkeypatch, {TEST})
    seen: dict = {}

    def fake_prep(repo_root, **kw):
        seen.update(kw)
        return {"error": None, "peer_held": list(kw.get("peer_held") or [])}

    monkeypatch.setattr(rp, "review_prep", fake_prep)
    monkeypatch.setattr(rp, "undeclared_edits_for_run", lambda *a, **kw: [])
    from coordinator_core.ops.dispatch_emit import land_run

    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda *a, **kw: {})
    params = {"run_id": RUN, "base": BASE, "plan_path": "docs/plans/p.md", "worktree": True, "session_id": "mine"}

    reply = rp._handle(params, tmp_path)

    assert TEST in reply["peer_held"], reply
    assert PROD in seen["paths"] and TEST in seen["paths"], seen


def test_nothing_declared_still_raises_without_a_peer_held_clause(tmp_path, monkeypatch):
    _stage(tmp_path, [])
    _peer_touches(monkeypatch, set())

    with pytest.raises(ValueError, match="no reviewable paths") as exc:
        rp.declared_paths_for_run(tmp_path, RUN, own_session="mine")
    assert "peer-held" not in str(exc.value)


def test_a_run_with_no_peer_touch_reviews_every_declared_path(tmp_path, monkeypatch):
    _stage(tmp_path, [PROD, TEST])
    _peer_touches(monkeypatch, set())

    assert rp.declared_paths_for_run(tmp_path, RUN, own_session="mine") == [PROD, TEST]
