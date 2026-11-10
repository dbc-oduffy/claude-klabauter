"""Two raw engine-ask submissions of the same text yield one run and one sizing: the second joins a live
holder, resumes a halted holder through its bound sizing, and a different text still mints fresh."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import launch_records, warp_live
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.extant_work import normalized_ask_hash

SIZING = "state/sizings/2026-11/2026-11-10-gapq4.yaml"
ASK = "Size the gapq4 ask"
REFIRE = "  size   THE gapq4\nask "


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "state" / "sizings" / "2026-11").mkdir(parents=True)
    managed = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return root


def _payload(root: Path, ask: str) -> dict:
    return {
        "tool_name": "Workflow",
        "cwd": str(root),
        "session_id": "s1",
        "transcript_path": str(root / "t.jsonl"),
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"ask": ask, "source": "pm"}},
    }


def _holders(root: Path) -> list[dict]:
    return [r for r in run_token.held_records(root) if r.get("ask_sha256")]


def _fire_first(root: Path, tmp_path: Path, sizing_rel: str | None = None) -> str:
    """Submit ASK through the hook, then mark its run fired as the harness would; returns its run id."""
    env = mod.evaluate(_payload(root, ASK))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    (record,) = _holders(root)
    run_id, script = record["run_id"], record["script_path"]
    (root / "t.jsonl").write_text("", encoding="utf-8")
    Path(launch_records.record_path(str(warp_live.managed_tempdir()), "s1", "w1")).write_text(
        json.dumps({"scriptPath": script, "fired_at": 1.0}), encoding="utf-8"
    )
    warp_live.record_submission(
        root, run_id, session_id="s1", transcript_path=str(root / "t.jsonl"), script_path=script,
        sizing_rel=sizing_rel, tool_use_id="tu1",
    )
    if sizing_rel:
        assert run_token.bind_sizing(root, run_id, sizing_rel)
    return run_id


def test_a_refire_of_a_live_ask_is_denied_naming_the_run_and_mints_nothing(repo, tmp_path):
    run_id = _fire_first(repo, tmp_path)
    env = mod.evaluate(_payload(repo, REFIRE))
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert f"Run {run_id} is already working this ask" in hso["permissionDecisionReason"]
    assert [r["run_id"] for r in _holders(repo)] == [run_id]
    assert list((repo / "state" / "sizings").rglob("*.yaml")) == []


def test_a_refire_of_a_halted_ask_resumes_its_bound_sizing_without_a_new_marker(repo, tmp_path, monkeypatch):
    (repo / SIZING).write_text("ask: x\n", encoding="utf-8")
    run_id = _fire_first(repo, tmp_path, sizing_rel=SIZING)
    (sessions := tmp_path / "claude" / "sessions").mkdir(parents=True)
    (sessions / "1.json").write_text(json.dumps({"sessionId": "s1", "pid": 2**22 + 12345}), encoding="utf-8")
    seen: dict = {}

    def resume(root, sizing_rel, payload, params, session_id, launch, **kw):
        seen.update(sizing_rel=sizing_rel, params=dict(params))
        return {"resumed": True}, None

    monkeypatch.setattr(mod, "_resume_route", resume)
    env = mod.evaluate(_payload(repo, REFIRE))
    assert env == {"resumed": True}
    assert seen["sizing_rel"] == SIZING and seen["params"]["sizing_path"] == SIZING
    assert {r["run_id"] for r in run_token.held_records(repo)} <= {run_id}


def test_a_different_ask_still_mints_a_fresh_run(repo, tmp_path):
    first = _fire_first(repo, tmp_path)
    env = mod.evaluate(_payload(repo, "Size another ask entirely"))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    by_hash = {r["ask_sha256"]: r["run_id"] for r in _holders(repo)}
    assert by_hash[normalized_ask_hash(ASK)] == first
    assert len(by_hash) == 2 and by_hash[normalized_ask_hash("Size another ask entirely")] != first


def test_the_premint_marker_records_the_normalized_ask_hash(repo):
    minted = mod._premint_marker({"ask": REFIRE, "session_id": "s1"}, repo)
    (record,) = _holders(repo)
    assert record["run_id"] == minted["run_id"] and record["ask_sha256"] == normalized_ask_hash(ASK)
