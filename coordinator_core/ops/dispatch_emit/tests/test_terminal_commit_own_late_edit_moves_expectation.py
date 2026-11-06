"""dispatch.terminal_commit moves the blob-drift expectation onto bytes the run's own record dir
holds (``kept.json`` / ``measured-rev.json``) when no touch ledger carries the writer's hash, as an
executor's Write/Edit does not; bytes matching none of the run's records still refuse."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, drift_attribution, kept_work, terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401 -- fixtures
    SID,
    _fire,
    _git,
    _record_at,
    _sha,
    repo,
)
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-1"


def _read_event(repo, rel: str, digest: str, at: float) -> None:
    """The expectation the session's ledger holds: a read of the pre-edit bytes."""
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID)
    touch_record.append_event(
        sink, session_id=SID, agent_id=None, verb="T", path=rel, content_hash=digest, kind="r", timestamp=at,
    )


def _keep(repo, rel: str, text: str) -> None:
    record_dir = ask_contract.record_dir(repo, RUN_ID)
    blob = kept_work.blob_sha(text.encode("utf-8"))
    assert kept_work.write(record_dir, {"C4": {"paths": {rel: blob}}}, None) is not None


def test_the_runs_own_unledgered_late_edit_lands(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _read_event(repo, "a.py", _sha("A\n"), 1000.0)
    _keep(repo, "a.py", "B\n")
    out, seen = _fire(repo, monkeypatch)
    assert out["committed"] is True, out
    assert [s["expected_content_sha256"] for s in seen] == [{"a.py": _sha("A\n")}, {"a.py": _sha("B\n")}]


def test_the_measured_rev_blob_owns_the_late_edit_too(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _read_event(repo, "a.py", _sha("A\n"), 1000.0)
    record_dir = ask_contract.record_dir(repo, RUN_ID)
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / ask_contract.MEASURED_REV_RECORD).write_text(
        json.dumps({"blobs": {"a.py": kept_work.blob_sha(b"B\n")}}), encoding="utf-8"
    )
    out, _ = _fire(repo, monkeypatch)
    assert out["committed"] is True, out


def test_a_different_sessions_rewrite_of_the_kept_path_still_refuses(repo, monkeypatch):
    (repo / "a.py").write_text("FOREIGN\n", encoding="utf-8", newline="")
    _read_event(repo, "a.py", _sha("A\n"), 1000.0)
    _keep(repo, "a.py", "B\n")
    head = _git(["rev-parse", "HEAD"], repo)
    out, seen = _fire(repo, monkeypatch)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert out["blob_drift"][0]["path"] == "a.py"
    assert len(seen) == 1
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_kept_blob_for_another_path_owns_nothing(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _read_event(repo, "a.py", _sha("A\n"), 1000.0)
    _keep(repo, "other.py", "B\n")
    out, _ = _fire(repo, monkeypatch)
    assert out["refused"] == "blob-drift"


def test_run_recorded_blobs_reads_only_the_asked_paths(tmp_path):
    kept_work.write(
        tmp_path, {"C1": {"paths": {"a.py": "b" * 40, "z.py": "c" * 40}}}, None, {"a.py": "d" * 40},
    )
    assert drift_attribution.run_recorded_blobs(tmp_path, ["./a.py"]) == {"a.py": ["b" * 40, "d" * 40]}
    assert drift_attribution.run_recorded_blobs(None, ["a.py"]) == {}
    assert drift_attribution.run_recorded_blobs(tmp_path / "missing", ["a.py"]) == {}


def test_own_drift_actuals_without_a_record_dir_is_unchanged(repo):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _record_at(repo, "a.py", _sha("B\n"), 1000.0)
    drift = [{"path": "a.py", "basis": "content-sha256", "expected": _sha("A\n"), "actual": _sha("B\n")}]
    assert terminal_commit._own_drift_actuals(repo, None, drift, ["a.py"], SID, None) == {"a.py": _sha("B\n")}
