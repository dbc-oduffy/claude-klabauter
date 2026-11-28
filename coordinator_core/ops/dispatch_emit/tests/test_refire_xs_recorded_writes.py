"""An XS sizing refire that names no writes stages the footprint its halted predecessor's manifest recorded."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_stage, requirements_ledger
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

SIZING_REL = "state/sizings/2026-10-16-fixture.yaml"


def _xs_sizing(repo):
    doc = {"intent": "Fix the thing", "exit_criterion": {"statement": "it works"}, "estimate": {"tshirt": "XS"}}
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


@pytest.fixture(autouse=True)
def _no_register_write(monkeypatch):
    """The ledger bind of a resumed run rewrites the sizing register under a git lock; the fixture repo has neither."""
    monkeypatch.setattr(requirements_ledger, "bind", lambda *a, **k: None)
    monkeypatch.setattr(ask_stage, "revisions_block", lambda *a, **k: "")


def _link_refire(repo, run_id, prior):
    rec = record_dir(repo, run_id)
    rec.mkdir(parents=True, exist_ok=True)
    (rec / requirements_ledger.RUN_FILE).write_text(
        json.dumps({requirements_ledger.RESUMED_FROM_FIELD: prior}) + "\n", encoding="utf-8"
    )


def test_refire_with_no_writes_stages_the_predecessors_recorded_footprint(tmp_path):
    _xs_sizing(tmp_path)
    write_run_scripts(tmp_path, "r0", "r1")
    first = stage(tmp_path, run_id="r0", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    _link_refire(tmp_path, "r1", "r0")
    again = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=[])
    assert [r.id for r in again.rows] == ["X1"]
    assert again.rows[0].writes == first.rows[0].writes
    assert "pkg/a.py" in again.rows[0].writes


def test_refire_with_explicit_writes_keeps_them(tmp_path):
    _xs_sizing(tmp_path)
    write_run_scripts(tmp_path, "r0", "r1")
    stage(tmp_path, run_id="r0", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    _link_refire(tmp_path, "r1", "r0")
    again = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/b.py"])
    assert "pkg/b.py" in again.rows[0].writes
    assert "pkg/a.py" not in again.rows[0].writes


def test_fresh_run_with_no_writes_is_still_refused(tmp_path):
    _xs_sizing(tmp_path)
    write_run_scripts(tmp_path, "r1")
    with pytest.raises(AskStageError, match="^XS-MINT-NO-WRITES: "):
        stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=[])


def test_refire_whose_predecessor_recorded_nothing_is_still_refused(tmp_path):
    _xs_sizing(tmp_path)
    write_run_scripts(tmp_path, "r1")
    _link_refire(tmp_path, "r1", "r0")
    with pytest.raises(AskStageError, match="^XS-MINT-NO-WRITES: "):
        stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=[])


_GATE_REL = "state/sizings/2026-10-15-xs-refire.yaml"


def _gate_repo(tmp_path):
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
    (tmp_path / _GATE_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return tmp_path


def _gate_record(root, run_id, writes, base="state/warp", row_id="X1"):
    d = root / base / run_id
    d.mkdir(parents=True)
    rows = [{"id": row_id, "agent_type": "x", "model": "m", "brief_path": "b", "writes": writes, "wave": 0}]
    (d / "manifest.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")


def test_sizing_xs_mint_newest_recorded_x1_writes_win(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import recorded_xs_writes

    root = _gate_repo(tmp_path)
    _gate_record(root, "warp-2026-10-15-xs-refire-20261015T010101-aaaaaa", ["old.py"])
    _gate_record(root, "warp-2026-10-15-xs-refire-20261015T020202-bbbbbb", ["new.py"], base="scratch/warp")
    assert recorded_xs_writes(root, _GATE_REL) == ["new.py"]


def _ask_record(root, run_id, writes, sizing_rel=_GATE_REL):
    _gate_record(root, run_id, writes)
    (root / "state/warp" / run_id / "run.json").write_text(json.dumps({"sizing_rel": sizing_rel}), encoding="utf-8")


def test_halted_ask_run_manifest_supplies_the_refire_footprint(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import recorded_xs_writes

    root = _gate_repo(tmp_path)
    _ask_record(root, "ask-20261015T030303-cccccc", ["pkg/ask.py", "pkg/tests/test_ask.py"])
    assert recorded_xs_writes(root, _GATE_REL) == ["pkg/ask.py", "pkg/tests/test_ask.py"]


def test_ask_run_bound_to_another_sizing_is_ignored(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import recorded_xs_writes

    root = _gate_repo(tmp_path)
    _ask_record(root, "ask-20261015T030303-cccccc", ["pkg/ask.py"], sizing_rel="state/sizings/2026-10-15-other.yaml")
    assert recorded_xs_writes(root, _GATE_REL) == []


def test_newest_run_wins_across_ask_and_warp_names(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import recorded_xs_writes

    root = _gate_repo(tmp_path)
    _gate_record(root, "warp-2026-10-15-xs-refire-20261015T010101-aaaaaa", ["old.py"])
    _ask_record(root, "ask-20261015T030303-cccccc", ["newer.py"])
    assert recorded_xs_writes(root, _GATE_REL) == ["newer.py"]


def test_emit_gate_seeds_the_refire_with_recorded_writes(tmp_path):
    from coordinator_core.ops.dispatch_emit import op

    root = _gate_repo(tmp_path)
    _gate_record(root, "warp-2026-10-15-xs-refire-20261015T020202-bbbbbb", ["pkg/a.py"])
    assert op._gate_sizing_at_emit(root, _GATE_REL, [])["writes"] == ["pkg/a.py"]
    assert op._gate_sizing_at_emit(root, _GATE_REL, ["pkg/b.py"])["writes"] == ["pkg/b.py"]


def test_other_sizing_runs_and_other_rows_are_ignored(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import recorded_xs_writes

    root = _gate_repo(tmp_path)
    _gate_record(root, "warp-2026-10-15-xs-refire-extra-20261015T010101-aaaaaa", ["other.py"])
    _gate_record(root, "warp-2026-10-15-xs-refire-20261015T020202-bbbbbb", ["s.py"], row_id="R1")
    assert recorded_xs_writes(root, _GATE_REL) == []


def test_first_fire_with_no_record_stays_unseeded(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import recorded_xs_writes

    root = _gate_repo(tmp_path)
    assert recorded_xs_writes(root, _GATE_REL) == []
    assert _op_gate(root, [])["writes"] == []


def _op_gate(root, writes):
    from coordinator_core.ops.dispatch_emit import op

    return op._gate_sizing_at_emit(root, _GATE_REL, writes)
