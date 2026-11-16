"""An errored test leg outranks measured-rev-drift at both ledger gates; a green leg still drifts."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run, requirements_ledger
from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

RID = "run-leg"
VERDICT = {"items": [{"id": "R1", "status": "met"}]}
LEG_ERROR = {"wave_not_run": [{"slot": 2, "agent_type": "dispatch.test_leg", "reason": "returned no review"}]}
OTHER_SLOT = {"wave_not_run": [{"slot": 1, "agent_type": "coordinator:code-reviewer", "reason": "skipped: x"}]}


def _drifted_run(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    record = measured_rev_record_path(tmp_path, RID)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"blobs": {"a.py": commit.checkin_blob_sha(tmp_path, "a.py")}, "removed": []}))
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    return record.parent


def _land_run_gate(root, monkeypatch, run_dir, review):
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda r, d: SimpleNamespace(items=[1], all_of_pm_ask=False))
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda r, d: [])
    monkeypatch.setattr(requirements_ledger, "judge", lambda *a, **k: {"pass": True, "failures": [], "open": []})
    return land_run._ledger_gate(root, run_dir, VERDICT, None, [], {}, None, review, None, None)[0]


def _reasons(refusal):
    return [f["reason"] for f in refusal["ledger_failures"]]


def test_land_run_gate_names_the_test_leg(tmp_path, monkeypatch):
    refusal = _land_run_gate(tmp_path, monkeypatch, _drifted_run(tmp_path), LEG_ERROR)
    assert refusal["refused"] == "ledger-unmet"
    assert all(r.startswith("test-leg-error: dispatch.test_leg returned no review") for r in _reasons(refusal))


def test_land_run_gate_green_leg_still_drifts(tmp_path, monkeypatch):
    for review in ({}, OTHER_SLOT):
        refusal = _land_run_gate(tmp_path, monkeypatch, _drifted_run(tmp_path), review)
        assert _reasons(refusal) == ["measured-rev-drift: a.py"]


def test_terminal_gate_names_the_test_leg(tmp_path):
    run_dir = _drifted_run(tmp_path)
    ledger = SimpleNamespace(items=[1], all_of_pm_ask=False)
    refusal = refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, ledger, LEG_ERROR)
    assert refusal["refused"] == "ledger-unmet"
    assert _reasons(refusal) == ["test-leg-error: dispatch.test_leg returned no review"
                                 "; the measurement is stale, not the requirement"]


def test_terminal_gate_green_leg_still_drifts(tmp_path):
    run_dir = _drifted_run(tmp_path)
    ledger = SimpleNamespace(items=[1], all_of_pm_ask=False)
    for review in (None, {}, OTHER_SLOT):
        refusal = refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, ledger, review)
        assert _reasons(refusal) == ["measured-rev-drift: a.py"]


def test_leg_error_without_drift_is_not_this_refusal(tmp_path):
    run_dir = _drifted_run(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ledger = SimpleNamespace(items=[1], all_of_pm_ask=False)
    assert refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, ledger, LEG_ERROR) is None


def test_unreadable_record_keeps_its_reason_under_a_leg_error(tmp_path):
    run_dir = _drifted_run(tmp_path)
    measured_rev_record_path(tmp_path, RID).write_text("{not json")
    ledger = SimpleNamespace(items=[1], all_of_pm_ask=False)
    refusal = refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, ledger, LEG_ERROR)
    assert _reasons(refusal) == ["measured-rev-record-unreadable"]
    assert land_run.drift_reason_for_leg("measured-rev-record-unreadable", LEG_ERROR) == "measured-rev-record-unreadable"
