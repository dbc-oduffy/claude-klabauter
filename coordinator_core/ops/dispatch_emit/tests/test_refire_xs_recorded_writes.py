"""An XS refire from a sizing reuses the X1 writes the earlier run recorded; it never invents them."""

from __future__ import annotations

import json

import yaml

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.sizing_xs_mint import recorded_xs_writes

REL = "state/sizings/2026-10-15-xs-refire.yaml"


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "fix the thing",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized", "interaction_mode": "pm",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "fix the thing"}],
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-15", "mode": "pm"}},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return tmp_path


def _record(root, run_id, writes, base="state/warp", row_id="X1"):
    d = root / base / run_id
    d.mkdir(parents=True)
    rows = [{"id": row_id, "agent_type": "x", "model": "m", "brief_path": "b", "writes": writes, "wave": 0}]
    (d / "manifest.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")


def test_newest_recorded_x1_writes_win(tmp_path):
    root = _repo(tmp_path)
    _record(root, "warp-2026-10-15-xs-refire-20261015T010101-aaaaaa", ["old.py"])
    _record(root, "warp-2026-10-15-xs-refire-20261015T020202-bbbbbb", ["new.py"], base="scratch/warp")
    assert recorded_xs_writes(root, REL) == ["new.py"]


def test_other_sizing_runs_and_other_rows_are_ignored(tmp_path):
    root = _repo(tmp_path)
    _record(root, "warp-2026-10-15-xs-refire-extra-20261015T010101-aaaaaa", ["other.py"])
    _record(root, "warp-2026-10-15-xs-refire-20261015T020202-bbbbbb", ["s.py"], row_id="R1")
    assert recorded_xs_writes(root, REL) == []


def test_no_record_yields_nothing(tmp_path):
    assert recorded_xs_writes(_repo(tmp_path), REL) == []


def test_emit_gate_seeds_the_refire_with_recorded_writes(tmp_path):
    root = _repo(tmp_path)
    _record(root, "warp-2026-10-15-xs-refire-20261015T020202-bbbbbb", ["pkg/a.py"])
    out = op._gate_sizing_at_emit(root, REL, [])
    assert out["writes"] == ["pkg/a.py"]


def test_emit_gate_keeps_explicit_writes(tmp_path):
    root = _repo(tmp_path)
    _record(root, "warp-2026-10-15-xs-refire-20261015T020202-bbbbbb", ["pkg/a.py"])
    assert op._gate_sizing_at_emit(root, REL, ["pkg/b.py"])["writes"] == ["pkg/b.py"]


def test_first_fire_with_no_record_stays_unseeded(tmp_path):
    assert op._gate_sizing_at_emit(_repo(tmp_path), REL, [])["writes"] == []
