"""A refire whose earlier attempt edited a declared path is not refused pre-existing-dirt for it, at the
land_run ledger gate and at terminal commit; foreign dirt on a declared path still refuses."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import land_run, measured_rev, own_checkpoint_paths, requirements_ledger
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow, StageManifest
from coordinator_core.ops.dispatch_emit.emit import _dispatch_report_path

PLAN = "docs/plans/2026-11/p.md"
OWN = "own.py"
FOREIGN = "foreign.py"
LEDGER = SimpleNamespace(items=(1,), all_of_pm_ask=False)
VERDICT = {"items": [{"id": "R1", "status": "met"}]}


def _tree(root, report_lines, *, prior=True):
    for n in (OWN, FOREIGN):
        (root / n).write_text("x\n")
    live = root / _dispatch_report_path(PLAN, "C1")
    live.parent.mkdir(parents=True)
    name = live.name[: -len(".md")] + (".prior.md" if prior else ".md")
    (live.parent / name).write_text("status: PARTIAL\n\ngit status:\n" + "\n".join(report_lines) + "\n")
    run = root / "state" / "warp" / "warp-x-aaaaaa-20261112T000000-bbbbbb"
    run.mkdir(parents=True)
    return run


def _terminal(root, run, paths):
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="b", writes=tuple(paths), wave=0, register_rows=("R1",),
    )
    m = StageManifest(
        run_dir="r", rows=(row,), review_declared_paths=(), marker_path="m", write_baseline=wb.capture(root, paths),
    )
    return tc._pre_existing_dirt_refusal(root, m, VERDICT, LEDGER, PLAN, run)


def _land(root, monkeypatch, run, paths):
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "write_baseline": wb.capture(root, paths),
        "rows": [{"id": "C1", "register_rows": ["R1"], "writes": list(paths)}],
    }))
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda *_a, **_k: SimpleNamespace(items=[], all_of_pm_ask=False))
    monkeypatch.setattr(requirements_ledger, "judge", lambda *_a, **_k: {"pass": True, "failures": [], "open": []})
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda *_a, **_k: [])
    monkeypatch.setattr(measured_rev, "measured_rev_drift", lambda *_a, **_k: [])
    monkeypatch.setattr(own_checkpoint_paths, "checkpoint_landed_paths", lambda *_a, **_k: frozenset())
    return land_run._ledger_gate(
        root, run, {"items": [{"id": "R1", "status": "met"}]}, manifest, [], {}, None,
        {"prep": {"run_base_sha": "abc"}}, None, PLAN,
    )[0]


def test_terminal_commit_credits_path_the_earlier_attempt_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_plan_checkpoint_touched", lambda *_a: frozenset())
    run = _tree(tmp_path, [OWN])
    assert _terminal(tmp_path, run, [OWN]) is None


def test_terminal_commit_still_refuses_foreign_dirt_beside_it(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_plan_checkpoint_touched", lambda *_a: frozenset())
    run = _tree(tmp_path, [OWN])
    out = _terminal(tmp_path, run, [OWN, FOREIGN])
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: foreign.py"


def test_terminal_commit_without_a_prior_report_the_path_is_dirt(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_plan_checkpoint_touched", lambda *_a: frozenset())
    run = _tree(tmp_path, [OWN], prior=False)
    out = _terminal(tmp_path, run, [OWN])
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: own.py"


def test_land_run_gate_credits_path_the_earlier_attempt_reported(tmp_path, monkeypatch):
    run = _tree(tmp_path, [OWN])
    assert _land(tmp_path, monkeypatch, run, [OWN]) is None


def test_land_run_gate_still_refuses_foreign_dirt_beside_it(tmp_path, monkeypatch):
    run = _tree(tmp_path, [OWN])
    out = _land(tmp_path, monkeypatch, run, [OWN, FOREIGN])
    assert out is not None and out["refused"] == "ledger-unmet"
    assert "pre-existing-dirt: foreign.py" in json.dumps(out)
    assert "pre-existing-dirt: own.py" not in json.dumps(out)


def test_report_naming_a_path_outside_the_rows_declared_writes_credits_nothing(tmp_path):
    _tree(tmp_path, [OWN, FOREIGN])
    assert wb.prior_attempt_reported(tmp_path, PLAN, [("C1", [OWN])]) == {OWN}
    assert wb.prior_attempt_reported(tmp_path, PLAN, [("C2", [OWN])]) == frozenset()
    assert wb.prior_attempt_reported(tmp_path, None, [("C1", [OWN])]) == frozenset()
