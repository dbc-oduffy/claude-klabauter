"""A dropped bounded checkin_blob_sha read falls back to the unbounded in-process hash; measure_unavailable only when that cannot hash either."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run, measured_rev as mr, requirements_ledger
from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_RECORD

VERDICT = {"items": [{"id": "R1", "status": "met"}]}


def _setup(tmp_path: Path, removed: tuple = ()) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.txt").write_text("one\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    blobs = {"a.txt": commit._checkin_blob_sha_read(tmp_path, "a.txt")}
    (run_dir / MEASURED_REV_RECORD).write_text(json.dumps({"blobs": blobs, "removed": list(removed)}))
    return run_dir


def _drop(monkeypatch: pytest.MonkeyPatch, *, times: int) -> list:
    """Make the first `times` reads answer None (a bounded_io stall), then read for real."""
    real, calls = commit.checkin_blob_sha, []

    def fake(root, rel):
        calls.append(rel)
        return None if len(calls) <= times else real(root, rel)

    monkeypatch.setattr(commit, "checkin_blob_sha", fake)
    return calls


def _drop_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commit, "_checkin_blob_sha_read", lambda root, rel: None)


def test_dropped_bounded_read_falls_back_to_unbounded_and_is_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    calls = _drop(monkeypatch, times=99)
    reads, real = [], commit._checkin_blob_sha_read
    monkeypatch.setattr(commit, "_checkin_blob_sha_read", lambda root, rel: reads.append(rel) or real(root, rel))
    assert mr.measured_rev_drift(run_dir, tmp_path) == []
    assert calls == ["a.txt"] and reads == ["a.txt"]


def test_fallback_read_of_a_changed_file_is_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    (tmp_path / "a.txt").write_text("two\n")
    _drop(monkeypatch, times=1)
    assert mr.measured_rev_drift(run_dir, tmp_path) == ["a.txt"]


def test_unhashable_path_raises_measure_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    _drop(monkeypatch, times=99)
    _drop_unbounded(monkeypatch)
    with pytest.raises(mr.MeasureUnavailable) as exc:
        mr.measured_rev_drift(run_dir, tmp_path)
    assert exc.value.paths == ["a.txt"] and isinstance(exc.value, OSError)


def test_truly_absent_recorded_path_stays_drift(tmp_path: Path) -> None:
    run_dir = _setup(tmp_path)
    (tmp_path / "a.txt").unlink()
    assert mr.measured_rev_drift(run_dir, tmp_path) == ["a.txt"]


def test_land_run_gate_names_measure_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _setup(tmp_path)
    _drop(monkeypatch, times=99)
    _drop_unbounded(monkeypatch)
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda r, d: SimpleNamespace(items=[1], all_of_pm_ask=False))
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda r, d: [])
    monkeypatch.setattr(requirements_ledger, "judge", lambda *a, **k: {"pass": True, "failures": [], "open": []})
    refusal, _, _ = land_run._ledger_gate(tmp_path, run_dir, VERDICT, None, [], {}, None, {}, None, None)
    reasons = [f["reason"] for f in refusal["ledger_failures"]]
    assert reasons == ["measure_unavailable: a.txt"]


def test_test_leg_failure_routes_measure_unavailable_to_test_infra() -> None:
    review = {"wave_not_run": [{"agent_type": land_run.TEST_LEG_AGENT_TYPE, "reason": "did not run"}]}
    out = land_run.test_infra_refusal("measure_unavailable: a.txt", review)
    assert out is not None and out["refused"] == "test-infra"
