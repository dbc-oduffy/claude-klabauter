"""A refire's ledger gate owns the resumed_from predecessor's recorded in-plan writes; foreign dirt
beside them still refuses."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import land_run, measured_rev, own_checkpoint_paths, requirements_ledger
from coordinator_core.ops.dispatch_emit import write_baseline as wb

PRED = "ask-20261027T135906-d0f621"
REFIRE = "warp-2026-10-27-american-spelling-ui-d0f621-20261027T143416-633153"
PLAN = "docs/plans/p.md"


def _records(root, outcome):
    pred = root / "state" / "warp" / PRED
    pred.mkdir(parents=True)
    (pred / "run-outcome.json").write_text(json.dumps(outcome))
    run = root / "state" / "warp" / REFIRE
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"resumed_from": PRED}))
    return run


SLICE = {"inline_review": {"prep": {"slice_files": ["a.py"]}, "tests": {"edited": ["t.py"], "created": ["n.py"]}}}


def test_prior_pass_written_credits_predecessor_slice_and_tests(tmp_path):
    run = _records(tmp_path, SLICE)
    assert {"a.py", "t.py", "n.py"} <= wb.prior_pass_written(tmp_path, run)


def test_include_predecessors_false_drops_predecessor_slice(tmp_path):
    run = _records(tmp_path, SLICE)
    assert not {"a.py", "t.py", "n.py"} & wb.prior_pass_written(tmp_path, run, include_predecessors=False)


def test_unsafe_run_name_credits_no_predecessor(tmp_path):
    _records(tmp_path, SLICE)
    odd = tmp_path / "odd name"
    odd.mkdir()
    assert wb.prior_pass_written(tmp_path, odd) == frozenset()


def _land_gate(tmp_path, monkeypatch, paths, run_dir, own=()):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "write_baseline": wb.capture(tmp_path, paths),
        "rows": [{"register_rows": ["R4"], "writes": list(paths)}],
    }))
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda *_a, **_k: SimpleNamespace(items=[], all_of_pm_ask=False))
    monkeypatch.setattr(requirements_ledger, "judge", lambda *_a, **_k: {"pass": True, "failures": [], "open": []})
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda *_a, **_k: [])
    monkeypatch.setattr(measured_rev, "measured_rev_drift", lambda *_a, **_k: [])
    monkeypatch.setattr(own_checkpoint_paths, "checkpoint_landed_paths", lambda *_a, **_k: frozenset(own))
    return land_run._ledger_gate(
        tmp_path, run_dir, {"items": [{"id": "R4", "status": "met"}]}, manifest, [], {}, None,
        {"prep": {"run_base_sha": "abc"}}, None, PLAN,
    )[0]


def test_gate_passes_predecessor_recorded_write(tmp_path, monkeypatch):
    run = _records(tmp_path, SLICE)
    (tmp_path / "a.py").write_text("x\n")
    assert _land_gate(tmp_path, monkeypatch, ["a.py"], run) is None


def test_gate_still_refuses_foreign_dirt_beside_predecessor_write(tmp_path, monkeypatch):
    run = _records(tmp_path, SLICE)
    for n in ("a.py", "foreign.py"):
        (tmp_path / n).write_text("x\n")
    out = _land_gate(tmp_path, monkeypatch, ["a.py", "foreign.py"], run)
    assert out is not None and out["refused"] == "ledger-unmet"
    assert "pre-existing-dirt: foreign.py" in json.dumps(out)
    assert "pre-existing-dirt: a.py" not in json.dumps(out)


def test_absent_path_is_not_dirt(tmp_path, monkeypatch):
    run = _records(tmp_path, SLICE)
    assert _land_gate(tmp_path, monkeypatch, ["absent_test.py"], run) is None


def test_path_written_by_own_checkpoint_is_not_dirt(tmp_path, monkeypatch):
    run = _records(tmp_path, {})
    (tmp_path / "c.py").write_text("x\n")
    assert _land_gate(tmp_path, monkeypatch, ["c.py"], run, own=["c.py"]) is None
