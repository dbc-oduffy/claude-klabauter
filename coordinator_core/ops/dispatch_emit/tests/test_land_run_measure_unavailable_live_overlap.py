"""A test-infra measure_unavailable refusal names the live peer run that also writes the unhashable path."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core import warp_live
from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run, measured_rev as mr, requirements_ledger, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_RECORD

VERDICT = {"items": [{"id": "R1", "status": "met"}]}
NO_REVIEW = {"wave_not_run": [{"agent_type": land_run.TEST_LEG_AGENT_TYPE, "reason": "did not run"}]}
SCRIPT = "scratch/warp/x.mjs"
CAUSE = "file size changed while it was read (being written)"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "locks"))
    (root / "a.txt").write_text("one\n")
    monkeypatch.setattr(commit, "checkin_blob_sha", lambda r, rel: None)
    monkeypatch.setattr(commit, "_checkin_blob_sha_read", lambda r, rel: None)
    monkeypatch.setattr(commit, "diagnose_checkin_blob", lambda r, rel: commit.BlobDiagnosis(None, CAUSE))
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda r, d: SimpleNamespace(items=[1], all_of_pm_ask=False))
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda r, d: [])
    monkeypatch.setattr(requirements_ledger, "judge", lambda *a, **k: {"pass": True, "failures": [], "open": []})
    return root


def _own_run(root: Path, run_id: str = "own") -> Path:
    run_dir = root / "scratch" / "warp" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / MEASURED_REV_RECORD).write_text(json.dumps({"blobs": {"a.txt": "0" * 40}, "removed": []}))
    return run_dir


def _live_peer(root: Path, run_id: str, writes: list[str]) -> None:
    run_token.mint(root, run_id, SCRIPT, session_id=None)
    d = root / "scratch" / "warp" / run_id
    d.mkdir(parents=True, exist_ok=True)
    manifest = {"rows": [{"id": "C1", "writes": writes}], "review_declared_paths": [], "doc_writes": []}
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _gate(root: Path, run_dir: Path) -> dict:
    refusal, _, _ = land_run._ledger_gate(root, run_dir, VERDICT, None, [], {}, None, NO_REVIEW, None, None)
    assert refusal is not None and refusal["refused"] == "test-infra"
    return refusal


def test_a_live_peer_writing_the_path_is_named_in_the_reason_and_payload(repo: Path) -> None:
    run_dir = _own_run(repo)
    _live_peer(repo, "peer-run", ["a.txt"])
    refusal = _gate(repo, run_dir)
    assert "a.txt" in refusal["error"] and "live run peer-run" in refusal["error"]
    assert "stale, not the requirement" not in refusal["error"]
    assert refusal["test_infra"]["live_overlap"] == {"a.txt": ["peer-run"]}
    assert "live run peer-run" in refusal["test_infra"]["reason"]


def test_no_live_peer_leaves_the_refusal_text_unchanged(repo: Path) -> None:
    run_dir = _own_run(repo)
    refusal = _gate(repo, run_dir)
    reason = f"measure_unavailable: a.txt ({CAUSE})"
    assert refusal["test_infra"] == {"leg": f"{land_run.TEST_LEG_AGENT_TYPE} did not run", "reason": reason, "rearm": land_run.TEST_LEG_AGENT_TYPE}
    assert reason in refusal["error"] and "the measurement is stale, not the requirement" in refusal["error"]


def test_a_live_peer_writing_another_path_and_the_own_run_are_not_named(repo: Path) -> None:
    run_dir = _own_run(repo)
    _live_peer(repo, "peer-run", ["other.txt"])
    _live_peer(repo, "own", ["a.txt"])
    refusal = _gate(repo, run_dir)
    assert "live_overlap" not in refusal["test_infra"]
    assert "live-run overlap" not in refusal["error"]


def test_overlap_clause_round_trips_and_plain_reason_parses_empty() -> None:
    reason = mr.with_live_overlap("measure_unavailable: a.txt (x)", {"a.txt": ["r1", "r2"], "b/c.py": ["r3"]})
    assert mr.parse_live_overlap(reason) == {"a.txt": ["r1", "r2"], "b/c.py": ["r3"]}
    assert mr.with_live_overlap("measure_unavailable: a.txt (x)", {}) == "measure_unavailable: a.txt (x)"
    assert mr.parse_live_overlap("measure_unavailable: a.txt (x)") == {}
