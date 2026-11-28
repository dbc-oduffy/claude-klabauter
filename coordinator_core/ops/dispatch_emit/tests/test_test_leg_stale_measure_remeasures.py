"""A stale measurement with the test leg returning no review is re-measured in-process before the test-infra refusal; surviving drift still refuses."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run, measured_rev as mr, requirements_ledger, terminal_commit_refusals
from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_RECORD

VERDICT = {"items": [{"id": "R1", "status": "met"}]}
NO_REVIEW = {"wave_not_run": [{"agent_type": land_run.TEST_LEG_AGENT_TYPE, "reason": "did not run"}]}
LEDGER = SimpleNamespace(items=[1], all_of_pm_ask=False)


def _setup(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.txt").write_bytes(b"one\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    blobs = {"a.txt": commit._checkin_blob_sha_read(tmp_path, "a.txt")}
    (run_dir / MEASURED_REV_RECORD).write_text(json.dumps({"blobs": blobs, "removed": []}))
    return run_dir


def _stale_first_read(monkeypatch: pytest.MonkeyPatch) -> list:
    """The ordinary measurement answers measure_unavailable; every later (wider) one reads for real."""
    real, calls = mr.measured_rev_drift, []

    def fake(run_dir, repo_root, reread_deadline=None):
        calls.append(reread_deadline)
        if len(calls) == 1:
            raise mr.MeasureUnavailable(["a.txt"])
        return real(run_dir, repo_root, reread_deadline)

    monkeypatch.setattr(mr, "measured_rev_drift", fake)
    return calls


def _gate(tmp_path: Path, run_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda r, d: LEDGER)
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda r, d: [])
    monkeypatch.setattr(requirements_ledger, "judge", lambda *a, **k: {"pass": True, "failures": [], "open": []})
    return land_run._ledger_gate(tmp_path, run_dir, VERDICT, None, [], {}, None, NO_REVIEW, None, None)[0]


def test_ledger_gate_remeasures_a_stale_unavailable_and_does_not_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    calls = _stale_first_read(monkeypatch)
    refusal = _gate(tmp_path, run_dir, monkeypatch)
    assert refusal is None
    assert calls == [None, mr.REMEASURE_DEADLINE_SECS]


def test_ledger_gate_still_refuses_when_unavailable_survives_the_remeasure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    monkeypatch.setattr(commit, "checkin_blob_sha", lambda root, rel: None)
    monkeypatch.setattr(commit, "_checkin_blob_sha_read", lambda root, rel: None)
    monkeypatch.setattr(
        commit, "diagnose_checkin_blob", lambda root, rel: commit.BlobDiagnosis(None, "file size changed while it was read (being written)")
    )
    refusal = _gate(tmp_path, run_dir, monkeypatch)
    assert refusal["refused"] == "test-infra"
    assert refusal["test_infra"]["reason"].startswith("measure_unavailable: a.txt (")


def test_ledger_gate_still_refuses_real_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"two\n")
    refusal = _gate(tmp_path, run_dir, monkeypatch)
    assert refusal["refused"] == "test-infra"
    assert refusal["test_infra"]["reason"].startswith("measured-rev-drift: a.txt")


def test_terminal_commit_refusal_remeasures_a_stale_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    _stale_first_read(monkeypatch)
    out = terminal_commit_refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, LEDGER, NO_REVIEW)
    assert out is None


def test_terminal_commit_refusal_still_refuses_when_unavailable_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    monkeypatch.setattr(commit, "checkin_blob_sha", lambda root, rel: None)
    monkeypatch.setattr(commit, "_checkin_blob_sha_read", lambda root, rel: None)
    monkeypatch.setattr(
        commit, "diagnose_checkin_blob", lambda root, rel: commit.BlobDiagnosis(None, "file size changed while it was read (being written)")
    )
    out = terminal_commit_refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, LEDGER, NO_REVIEW)
    assert out["refused"] == "test-infra" and out["test_infra"]["reason"].startswith("measure_unavailable: a.txt (")


def _record_with(run_dir: Path, rel: str) -> None:
    record = run_dir / MEASURED_REV_RECORD
    blobs = json.loads(record.read_text())["blobs"]
    record.write_text(json.dumps({"blobs": {**blobs, rel: "0" * 40}, "removed": []}))


def _unhashable(monkeypatch: pytest.MonkeyPatch, rel: str) -> None:
    real, real_read, real_diag = commit.checkin_blob_sha, commit._checkin_blob_sha_read, commit.diagnose_checkin_blob
    monkeypatch.setattr(commit, "_checkin_blob_sha_read", lambda root, p: None if p == rel else real_read(root, p))
    monkeypatch.setattr(commit, "checkin_blob_sha", lambda root, p: None if p == rel else real(root, p))
    monkeypatch.setattr(
        commit, "diagnose_checkin_blob", lambda root, p: commit.BlobDiagnosis(None, "unreadable") if p == rel else real_diag(root, p)
    )


@pytest.mark.parametrize("exists", [False, True])
def test_an_evicted_predecessor_run_path_is_absent_not_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exists: bool) -> None:
    run_dir = _setup(tmp_path)
    rel = "state/warp/other-run-b16447/test-leg.json"
    if exists:
        (tmp_path / rel).parent.mkdir(parents=True)
        (tmp_path / rel).write_text("{}")
    _record_with(run_dir, rel)
    _unhashable(monkeypatch, rel)
    assert mr.measured_rev_drift(run_dir, tmp_path) == []
    assert terminal_commit_refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, LEDGER, NO_REVIEW) is None
    assert _gate(tmp_path, run_dir, monkeypatch) is None


def test_an_own_run_unhashable_path_still_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    rel = "state/warp/run/test-leg.json"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("{}")
    _record_with(run_dir, rel)
    _unhashable(monkeypatch, rel)
    with pytest.raises(mr.MeasureUnavailable):
        mr.measured_rev_drift(run_dir, tmp_path)
    out = terminal_commit_refusals._measured_rev_drift_refusal(run_dir, tmp_path, VERDICT, LEDGER, NO_REVIEW)
    assert out["refused"] == "test-infra" and rel in out["test_infra"]["reason"]


def test_a_now_passing_test_path_clears_on_the_refire_remeasure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    real, calls = mr.measured_rev_drift, []
    monkeypatch.setattr(mr, "measured_rev_drift", lambda d, r, w=None: calls.append(w) or real(d, r, w))
    assert land_run.remeasure_stale_gap("measure_unavailable: a.txt", NO_REVIEW, run_dir, tmp_path) is None
    assert calls == [mr.REMEASURE_DEADLINE_SECS]


def test_a_ran_test_leg_is_not_remeasured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    calls = _stale_first_read(monkeypatch)
    assert land_run.remeasure_stale_gap("measure_unavailable: a.txt", {}, run_dir, tmp_path) == "measure_unavailable: a.txt"
    assert calls == []
