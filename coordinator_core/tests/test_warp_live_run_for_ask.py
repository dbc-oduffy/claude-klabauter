"""warp_live.run_for_ask: finds the live or halted run holding a raw ask's normalized hash over held records only."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core import launch_records, warp_live
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.extant_work import normalized_ask_hash

SCRIPT = "scratch/warp/x.mjs"
SIZING = "state/sizings/mine.yaml"
ASK_HASH = normalized_ask_hash("Size the gapq4 ask")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    Path(launch_records.record_path(str(managed), "s1", "w1")).write_text(
        json.dumps({"scriptPath": SCRIPT, "fired_at": 1.0}), encoding="utf-8"
    )
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config / "sessions"


def _fire(repo, tmp_path, run_id="r1", **mint_kwargs):
    run_token.mint(repo, run_id, SCRIPT, session_id="s1", **mint_kwargs)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    warp_live.record_submission(
        repo, run_id, session_id="s1", transcript_path=str(transcript), script_path=SCRIPT,
        sizing_rel=mint_kwargs.get("sizing_rel"), tool_use_id="tu1",
    )
    return str(transcript)


def _find(repo, transcript, ask_hash=ASK_HASH):
    return warp_live.run_for_ask(repo, ask_hash, session_id="s1", transcript_path=transcript)


def test_a_live_holder_reads_live_with_its_run_id(repo, tmp_path, sessions):
    transcript = _fire(repo, tmp_path, ask_sha256=ASK_HASH)
    assert _find(repo, transcript) == ("live", "r1", None)


def test_a_different_hash_finds_nothing(repo, tmp_path, sessions):
    transcript = _fire(repo, tmp_path, ask_sha256=ASK_HASH)
    assert _find(repo, transcript, normalized_ask_hash("another ask")) is None


def test_a_not_live_holder_with_a_bound_sizing_reads_halted(repo, tmp_path, sessions):
    transcript = _fire(repo, tmp_path, ask_sha256=ASK_HASH, sizing_rel=SIZING)
    sessions.mkdir(parents=True)
    (sessions / "1.json").write_text(json.dumps({"sessionId": "s1", "pid": 2**22 + 12345}), encoding="utf-8")
    assert _find(repo, transcript) == ("halted", "r1", SIZING)


def test_a_not_live_holder_without_a_sizing_finds_nothing(repo, tmp_path, sessions):
    transcript = _fire(repo, tmp_path, ask_sha256=ASK_HASH)
    sessions.mkdir(parents=True)
    (sessions / "1.json").write_text(json.dumps({"sessionId": "s1", "pid": 2**22 + 12345}), encoding="utf-8")
    assert _find(repo, transcript) is None


def test_an_unfired_marker_holds_no_claim_on_the_ask(repo, sessions):
    run_token.mint(repo, "r1", "scratch/warp/never-fired.mjs", session_id="s1", marker=True, ask_sha256=ASK_HASH)
    assert _find(repo, None) is None


def test_the_lookup_spawns_nothing_and_walks_nothing(repo, tmp_path, sessions, monkeypatch):
    transcript = _fire(repo, tmp_path, ask_sha256=ASK_HASH)

    def _boom(*_a, **_k):
        raise AssertionError("run_for_ask must not spawn or walk")

    with monkeypatch.context() as patched:
        patched.setattr(subprocess, "Popen", _boom)
        patched.setattr(os, "walk", _boom)
        assert _find(repo, transcript) == ("live", "r1", None)
