"""A test leg that returned no review halts as a typed ``test-infra`` refusal naming ``dispatch.test_leg`` at both
ledger gates, never ``ledger-unmet`` on the requirement rows; a leg that ran still drifts ``ledger-unmet``."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_refusal_registry, land_run, requirements_ledger
from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

RID = "run-leg"
VERDICT = {"items": [{"id": "R1", "status": "met"}, {"id": "R3", "status": "met"}]}
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


def _assert_typed_test_infra(refusal):
    assert refusal["refused"] == "test-infra"
    assert "ledger_failures" not in refusal
    assert refusal["test_infra"]["rearm"] == "dispatch.test_leg"
    assert "Re-arm dispatch.test_leg" in refusal["error"]
    assert "the measurement is stale, not the requirement" in refusal["error"]


def test_land_run_gate_halts_test_infra_naming_the_test_leg(tmp_path, monkeypatch):
    _assert_typed_test_infra(_land_run_gate(tmp_path, monkeypatch, _drifted_run(tmp_path), LEG_ERROR))


def test_land_run_gate_green_leg_still_drifts(tmp_path, monkeypatch):
    for review in ({}, OTHER_SLOT):
        refusal = _land_run_gate(tmp_path, monkeypatch, _drifted_run(tmp_path), review)
        assert refusal["refused"] == "ledger-unmet"
        assert _reasons(refusal) == ["measured-rev-drift: a.py"] * 2


def test_terminal_gate_halts_test_infra_naming_the_test_leg(tmp_path):
    run_dir = _drifted_run(tmp_path)
    ledger = SimpleNamespace(items=[1], all_of_pm_ask=False)
    _assert_typed_test_infra(refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, ledger, LEG_ERROR))


def test_terminal_gate_green_leg_still_drifts(tmp_path):
    run_dir = _drifted_run(tmp_path)
    ledger = SimpleNamespace(items=[1], all_of_pm_ask=False)
    for review in (None, {}, OTHER_SLOT):
        refusal = refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, ledger, review)
        assert refusal["refused"] == "ledger-unmet"
        assert _reasons(refusal) == ["measured-rev-drift: a.py"] * 2


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
    assert refusal["refused"] == "ledger-unmet"
    assert _reasons(refusal) == ["measured-rev-record-unreadable"] * 2
    assert land_run.test_infra_refusal("measured-rev-record-unreadable", LEG_ERROR) is None


def test_test_infra_kind_is_registered_advisory():
    entry = land_refusal_registry.classify("test-infra")
    assert entry is not None and entry.denies is False and entry.harm is None and not entry.engine_bug


GREEN_REVERIFY = {"status_before": "fail", "status_after": "pass", "failing": [], "tests_run": 306}


def test_land_run_gate_commits_on_a_green_reverify_when_the_leg_left_no_review(tmp_path, monkeypatch):
    review = {**LEG_ERROR, "reverify": GREEN_REVERIFY}
    assert _land_run_gate(tmp_path, monkeypatch, _drifted_run(tmp_path), review) is None


def test_terminal_gate_commits_on_a_green_reverify_when_the_leg_left_no_review(tmp_path):
    run_dir = _drifted_run(tmp_path)
    ledger = SimpleNamespace(items=[1], all_of_pm_ask=False)
    review = {**LEG_ERROR, "reverify": GREEN_REVERIFY}
    assert refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, ledger, review) is None


def test_a_red_or_absent_reverify_keeps_the_test_infra_refusal(tmp_path, monkeypatch):
    for reverify in ({}, {"status_after": "fail", "failing": ["t::x"]}, {"status_after": "not_run"}):
        review = {**LEG_ERROR, "reverify": reverify}
        _assert_typed_test_infra(_land_run_gate(tmp_path, monkeypatch, _drifted_run(tmp_path), review))
