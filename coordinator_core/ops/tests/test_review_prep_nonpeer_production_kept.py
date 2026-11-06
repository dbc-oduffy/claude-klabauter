"""Review prep keeps a run's production file when the run's own firing session wrote it and a true peer touched only the test."""

import json

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.session import claim_index, liveness

RUN = "ask-20261106T002934-26c3fc"
PROD = "pkg/mod.py"
TEST = "pkg/tests/test_mod.py"
FIRING = "firing-session"
PEER = "456dea75"
CALLER = "review-caller"


class _Found(dict):
    recorded_kind = None


def _stage(tmp_path):
    base = record_dir(tmp_path, RUN)
    base.mkdir(parents=True, exist_ok=True)
    (base / "manifest.json").write_text(json.dumps({"review_declared_paths": [PROD, TEST]}), encoding="utf-8")


def _world(monkeypatch, table):
    def lookup(paths, **kw):
        found = _Found({p: table.get(p, []) for p in paths})
        found.recorded_kind = {p: {s: "w" for s in table.get(p, [])} for p in paths}
        return found

    monkeypatch.setattr(claim_index, "lookup", lookup)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd=None: False)
    monkeypatch.setattr(warp_run_deliverable, "live_run_write_claims", lambda *a, **kw: {})
    monkeypatch.setattr(run_token, "_read", lambda path: {"session_id": FIRING})


def test_firing_session_write_of_production_is_kept_while_peer_test_touch_drops_the_test(tmp_path, monkeypatch):
    _stage(tmp_path)
    _world(monkeypatch, {PROD: [FIRING], TEST: [FIRING, PEER]})
    assert rp.declared_paths_for_run(tmp_path, RUN, own_session=CALLER) == [PROD]


def test_a_true_peer_touch_on_production_still_drops_it(tmp_path, monkeypatch):
    _stage(tmp_path)
    _world(monkeypatch, {PROD: [PEER], TEST: [FIRING]})
    assert rp.declared_paths_for_run(tmp_path, RUN, own_session=CALLER) == [TEST]


def test_every_path_held_by_a_true_peer_still_halts(tmp_path, monkeypatch):
    _stage(tmp_path)
    _world(monkeypatch, {PROD: [PEER], TEST: [PEER]})
    with pytest.raises(ValueError, match="no reviewable paths"):
        rp.declared_paths_for_run(tmp_path, RUN, own_session=CALLER)
