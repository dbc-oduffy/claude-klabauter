"""A refire's own path set includes the paths its resumed_from predecessor withheld as halt-record-only:
land_run's and terminal_commit's pre-existing-dirt checks accept them, and foreign dirt beside them
still refuses."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import land_run, measured_rev, own_checkpoint_paths, requirements_ledger
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow, StageManifest

PRED = "ask-20261027T135906-d0f621"
REFIRE = "warp-2026-10-27-american-spelling-ui-d0f621-20261027T143416-633153"
PLAN = "docs/plans/p.md"
LEDGER = SimpleNamespace(items=(1,), all_of_pm_ask=False)
VERDICT = {"items": [{"id": "R1", "status": "met"}]}


def _records(root, withheld):
    pred = root / "state" / "warp" / PRED
    pred.mkdir(parents=True)
    (pred / "halted.json").write_text(json.dumps({"run_artifacts": {"withheld": {p: "halt-record-only" for p in withheld}}}))
    run = root / "state" / "warp" / REFIRE
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"resumed_from": PRED}))
    return run


def _write(root, names):
    for n in names:
        (root / n).write_text("x\n")


def test_prior_pass_written_credits_the_predecessors_withheld_paths(tmp_path):
    run = _records(tmp_path, ["a.py"])
    assert "a.py" in wb.prior_pass_written(tmp_path, run)


def test_prior_pass_written_credits_a_grandpredecessors_withheld_paths(tmp_path):
    run = _records(tmp_path, [])
    grand = "ask-20261027T120000-aaaaaa"
    (tmp_path / "state" / "warp" / PRED / "run.json").write_text(json.dumps({"resumed_from": grand}))
    gdir = tmp_path / "state" / "warp" / grand
    gdir.mkdir(parents=True)
    (gdir / "halted.json").write_text(json.dumps({"run_artifacts": {"withheld": {"g.py": "halt-record-only"}}}))
    assert "g.py" in wb.prior_pass_written(tmp_path, run)


def _land_gate(tmp_path, monkeypatch, paths, run_dir):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "write_baseline": wb.capture(tmp_path, paths),
        "rows": [{"register_rows": ["R4"], "writes": list(paths)}],
    }))
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda *_a, **_k: SimpleNamespace(items=[], all_of_pm_ask=False))
    monkeypatch.setattr(requirements_ledger, "judge", lambda *_a, **_k: {"pass": True, "failures": [], "open": []})
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda *_a, **_k: [])
    monkeypatch.setattr(measured_rev, "measured_rev_drift", lambda *_a, **_k: [])
    monkeypatch.setattr(own_checkpoint_paths, "checkpoint_landed_paths", lambda *_a, **_k: frozenset())
    return land_run._ledger_gate(
        tmp_path, run_dir, {"items": [{"id": "R4", "status": "met"}]}, manifest, [], {}, None,
        {"prep": {"run_base_sha": "abc"}}, None, PLAN,
    )[0]


def test_land_run_passes_a_predecessors_withheld_path(tmp_path, monkeypatch):
    run = _records(tmp_path, ["a.py"])
    _write(tmp_path, ["a.py"])
    assert _land_gate(tmp_path, monkeypatch, ["a.py"], run) is None


def test_land_run_still_refuses_foreign_dirt_beside_withheld(tmp_path, monkeypatch):
    run = _records(tmp_path, ["a.py"])
    _write(tmp_path, ["a.py", "foreign.py"])
    out = _land_gate(tmp_path, monkeypatch, ["a.py", "foreign.py"], run)
    assert out is not None and out["refused"] == "ledger-unmet"
    assert "pre-existing-dirt: foreign.py" in json.dumps(out)
    assert "pre-existing-dirt: a.py" not in json.dumps(out)


def _manifest(root, names):
    _write(root, names)
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="b", writes=tuple(names), wave=0,
        register_rows=("R1",),
    )
    return StageManifest(
        run_dir="r", rows=(row,), review_declared_paths=(), marker_path="m",
        write_baseline=wb.capture(root, names),
    )


def test_terminal_commit_passes_a_predecessors_withheld_path(tmp_path, monkeypatch):
    run = _records(tmp_path, ["a.py"])
    m = _manifest(tmp_path, ["a.py"])
    monkeypatch.setattr(tc, "_plan_checkpoint_touched", lambda root, plan, paths: frozenset())
    assert tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, PLAN, run) is None


def test_terminal_commit_still_refuses_foreign_dirt_beside_withheld(tmp_path, monkeypatch):
    run = _records(tmp_path, ["a.py"])
    m = _manifest(tmp_path, ["a.py", "foreign.py"])
    monkeypatch.setattr(tc, "_plan_checkpoint_touched", lambda root, plan, paths: frozenset())
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, PLAN, run)
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: foreign.py"
