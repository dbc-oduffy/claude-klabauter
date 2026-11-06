"""declared_paths_for_run keeps a run's other paths when one declared path has a live peer touch."""

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


class _Found(dict):
    recorded_kind = None


def _stage(tmp_path, declared):
    base = record_dir(tmp_path, RUN)
    base.mkdir(parents=True, exist_ok=True)
    (base / "manifest.json").write_text(json.dumps({"review_declared_paths": declared}), encoding="utf-8")


def _claims(monkeypatch, table):
    def lookup(paths, **kw):
        found = _Found({p: table.get(p, []) for p in paths})
        found.recorded_kind = {p: {s: "write" for s in table.get(p, [])} for p in paths}
        return found

    monkeypatch.setattr(claim_index, "lookup", lookup)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd=None: False)
    monkeypatch.setattr(warp_run_deliverable, "live_run_write_claims", lambda *a, **kw: {})


def test_peer_touch_on_the_test_only_keeps_the_production_file(tmp_path, monkeypatch):
    _stage(tmp_path, [PROD, TEST])
    _claims(monkeypatch, {TEST: [PEER]})
    assert rp.declared_paths_for_run(tmp_path, RUN, own_session="mine") == [PROD]


def test_every_path_peer_held_raises_naming_each_path_and_peer(tmp_path, monkeypatch):
    _stage(tmp_path, [PROD, TEST])
    _claims(monkeypatch, {PROD: [PEER], TEST: [PEER]})
    with pytest.raises(ValueError, match="no reviewable paths") as exc:
        rp.declared_paths_for_run(tmp_path, RUN, own_session="mine")
    text = str(exc.value)
    assert PROD in text and TEST in text and PEER in text


def test_nothing_declared_raises_without_a_peer_held_clause(tmp_path, monkeypatch):
    _stage(tmp_path, [])
    _claims(monkeypatch, {})
    with pytest.raises(ValueError, match="no reviewable paths") as exc:
        rp.declared_paths_for_run(tmp_path, RUN, own_session="mine")
    assert "peer-held" not in str(exc.value)


def test_own_session_touch_on_the_test_is_not_a_peer_hold(tmp_path, monkeypatch):
    _stage(tmp_path, [PROD, TEST])
    _claims(monkeypatch, {TEST: ["mine"]})
    assert rp.declared_paths_for_run(tmp_path, RUN, own_session="mine") == [PROD, TEST]


def test_peer_claim_outside_the_declared_set_changes_nothing(tmp_path, monkeypatch):
    _stage(tmp_path, [PROD, TEST])
    _claims(monkeypatch, {"pkg/other.py": [PEER]})
    assert rp.declared_paths_for_run(tmp_path, RUN, own_session="mine") == [PROD, TEST]

