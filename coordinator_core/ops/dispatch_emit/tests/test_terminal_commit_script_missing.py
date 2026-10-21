"""dispatch.terminal_commit lands a part whose script is gone from its receipt's landing markers."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import MARKER_PREFIX
from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture
def env(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_fixture(parent)
    outbox = parent / "lessons-outbox"
    outbox.mkdir()
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )
    return repo, script


def _call(repo: Path, script: str) -> dict:
    return terminal_commit._handler(
        {
            "script_path": script,
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
        },
        repo_root=repo / ".git",
    )


def _write_receipt(repo: Path, script: str, **fields) -> Path:
    path = emission_receipt_path(repo / script)
    path.write_text(
        json.dumps({"emitted_at": "2026-10-21T10:00:00", **fields}), encoding="utf-8"
    )
    return path


def _markers(repo: Path, script: str) -> list:
    text = (repo / script).read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.startswith(MARKER_PREFIX)]


def test_script_gone_lands_from_receipt_and_stamps_it_landed(env):
    repo, script = env
    markers = _markers(repo, script)
    assert markers
    receipt = _write_receipt(repo, script, landing_markers=markers)
    (repo / script).unlink()

    out = _call(repo, script)

    assert out["committed"] is True and out["sha"], out
    assert out["script_source"] == "receipt"
    assert json.loads(receipt.read_text(encoding="utf-8"))["landed"]["sha"] == out["sha"]


def test_receipt_without_markers_refuses_script_missing(env):
    repo, script = env
    receipt = _write_receipt(repo, script, landing_markers=[])
    (repo / script).unlink()

    out = _call(repo, script)

    assert out["refused"] == "script-missing" and out["committed"] is False
    assert "stranded_part_rebuild" in out["error"] and script in out["error"]
    assert "landed" not in json.loads(receipt.read_text(encoding="utf-8"))


def test_no_receipt_refuses_script_missing(env):
    repo, script = env
    (repo / script).unlink()
    assert _call(repo, script)["refused"] == "script-missing"


def test_present_script_is_unchanged(env):
    repo, script = env
    out = _call(repo, script)
    assert out["committed"] is True, out
    assert out["script_source"] == "script"
