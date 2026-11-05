"""record_terminal_judge_at and the stamp_run_stage_at landed seam."""
import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as c


def _outcome(path, review):
    (path / c.RUN_OUTCOME_FILE).write_text(json.dumps({"inline_review": review, "plan_path": "p.md"}))


def _criterion(path):
    return json.loads((path / c.RUN_OUTCOME_FILE).read_text())["inline_review"]["criterion"]


def test_missing_outcome_ends_not_met(tmp_path):
    c.stamp_run_stage_at(tmp_path, "landed")
    crit = _criterion(tmp_path)
    assert crit["status"] == "not_met" and crit["authored_by"] == c.ENGINE_JUDGE_AUTHOR
    assert crit["prior_status"] is None


def test_malformed_outcome_ends_not_met(tmp_path):
    (tmp_path / c.RUN_OUTCOME_FILE).write_text("{nope")
    assert c.record_terminal_judge_at(tmp_path) is True
    assert _criterion(tmp_path)["status"] == "not_met"


def test_no_criterion_keeps_other_keys(tmp_path):
    _outcome(tmp_path, {"verdict": "x"})
    c.stamp_run_stage_at(tmp_path, "landed")
    data = json.loads((tmp_path / c.RUN_OUTCOME_FILE).read_text())
    assert data["plan_path"] == "p.md" and data["inline_review"]["verdict"] == "x"
    assert data["inline_review"]["criterion"]["status"] == "not_met"


def test_indeterminate_ends_not_met_with_prior(tmp_path):
    _outcome(tmp_path, {"criterion": {"status": "indeterminate", "note": "n"}})
    c.stamp_run_stage_at(tmp_path, "landed")
    crit = _criterion(tmp_path)
    assert crit["status"] == "not_met" and crit["prior_status"] == "indeterminate" and crit["note"] == "n"


@pytest.mark.parametrize("status", ["met", "not_met"])
def test_verdict_records_stay_byte_identical(tmp_path, status):
    _outcome(tmp_path, {"criterion": {"status": status}})
    before = (tmp_path / c.RUN_OUTCOME_FILE).read_bytes()
    assert c.record_terminal_judge_at(tmp_path) is False
    c.stamp_run_stage_at(tmp_path, "landed")
    assert (tmp_path / c.RUN_OUTCOME_FILE).read_bytes() == before


def test_non_landed_stage_writes_no_verdict(tmp_path):
    c.stamp_run_stage_at(tmp_path, "staged")
    assert not (tmp_path / c.RUN_OUTCOME_FILE).exists()


def test_write_run_outcome_keeps_engine_not_met(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "record_dir", lambda repo_root, run_id: tmp_path)
    c.stamp_run_stage_at(tmp_path, "landed")
    c.write_run_outcome(tmp_path, "r", {"inline_review": {"verdict": "later"}})
    assert _criterion(tmp_path)["authored_by"] == c.ENGINE_JUDGE_AUTHOR


def test_write_run_outcome_incoming_verdict_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "record_dir", lambda repo_root, run_id: tmp_path)
    c.stamp_run_stage_at(tmp_path, "landed")
    c.write_run_outcome(tmp_path, "r", {"inline_review": {"criterion": {"status": "met"}}})
    assert _criterion(tmp_path) == {"status": "met"}


def test_write_run_outcome_does_not_keep_non_engine_criterion(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "record_dir", lambda repo_root, run_id: tmp_path)
    _outcome(tmp_path, {"criterion": {"status": "met", "authored_by": "judge"}})
    c.write_run_outcome(tmp_path, "r", {"inline_review": {"verdict": "x"}})
    data = json.loads((tmp_path / c.RUN_OUTCOME_FILE).read_text())
    assert "criterion" not in data["inline_review"]
