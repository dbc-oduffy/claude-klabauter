"""A waiver-bearing close writes the wsc receipt, and the close commit stages it."""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from coordinator_core.ops.ceremony import receipt_schema
from coordinator_core.session import core as session_core
from coordinator_core.workstream_complete import WAIVER_KEYS, apply as wsc_apply
from coordinator_core.workstream_complete import directives_commit_tail

_RealDatetime = datetime.datetime
SID = "test-sid-waiver-receipt-01"
SHAPE = {"disposition": "closed", "consumed_handoff": ""}


def _repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    session_core.reset_sessions_dir_cache()
    session_core.init(SID, "test goal", str(repo_root))
    return repo_root


def _emit(repo_root, decisions, sid=SID, stamp="2026-09-30T00:00:00Z", monkeypatch=None):
    fixed = _RealDatetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )

    class _Clock(_RealDatetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(datetime, "datetime", _Clock)
    return wsc_apply._emit_waiver_receipt(repo_root, decisions, sid, SHAPE)


def test_no_waiver_writes_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    assert _emit(repo, {}, monkeypatch=monkeypatch) is None
    assert not (repo / "state" / "ceremony").exists()


def test_empty_lists_write_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    decisions = {k: [] for k in WAIVER_KEYS}
    assert _emit(repo, decisions, monkeypatch=monkeypatch) is None
    assert not (repo / "state" / "ceremony").exists()


def test_no_sid_returns_none(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    decisions = {WAIVER_KEYS[0]: ["a"]}
    assert _emit(repo, decisions, sid="", monkeypatch=monkeypatch) is None
    assert not (repo / "state" / "ceremony").exists()


def test_one_key_written(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    rel = _emit(repo, {WAIVER_KEYS[0]: ["b", "a"]}, monkeypatch=monkeypatch)
    assert rel and "\\" not in rel
    receipt = json.loads((repo / rel).read_text(encoding="utf-8"))
    assert receipt["waivers"] == {WAIVER_KEYS[0]: ["a", "b"]}


def test_both_keys_sorted_deduped_schema_valid(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    decisions = {k: ["z", "y", "z"] for k in WAIVER_KEYS}
    rel = _emit(repo, decisions, monkeypatch=monkeypatch)
    receipt = json.loads((repo / rel).read_text(encoding="utf-8"))
    assert receipt["waivers"] == {k: ["y", "z"] for k in WAIVER_KEYS}
    assert receipt_schema.validate(receipt) == []


def test_two_closes_same_sid_two_files(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    first = _emit(repo, {WAIVER_KEYS[0]: ["a"]}, stamp="2026-09-30T00:00:00Z", monkeypatch=monkeypatch)
    before = (repo / first).read_bytes()
    second = _emit(repo, {WAIVER_KEYS[0]: ["b"]}, stamp="2026-09-30T00:05:00Z", monkeypatch=monkeypatch)
    assert first != second
    assert (repo / first).read_bytes() == before
    assert (repo / second).exists()


def test_popen_failure_does_not_stop_the_write(tmp_path, monkeypatch):
    repo = _repo(tmp_path)

    def _boom(*a, **k):
        raise OSError("no spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    rel = _emit(repo, {WAIVER_KEYS[0]: ["a"]}, monkeypatch=monkeypatch)
    assert rel and (repo / rel).exists()


def test_commit_tail_stages_receipt_once(tmp_path, monkeypatch):
    captured: dict = {}

    def _fake_commit(worktree_root, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            commit_failed=False,
            committed_sha="abc",
            pushed=False,
            integrity_breach=None,
            diagnostics=[],
        )

    monkeypatch.setattr(directives_commit_tail, "run_close_commit_and_release_claims", _fake_commit)
    monkeypatch.setattr(directives_commit_tail, "resolve_ship_stamp_candidates", lambda *a, **k: [])
    monkeypatch.setattr(directives_commit_tail, "resolve_close_stamp_candidates", lambda *a, **k: [])
    receipt = "state/ceremony/wsc/x-2026.json"
    wsc_apply._run_close_commit_tail(
        tmp_path,
        {"subject": "close", "stage_paths": [receipt]},
        SID,
        None,
        extra_stage_paths=[receipt, "state/ceremony/wsc/y.json"],
    )
    staged = list(captured["stage_paths"])
    assert staged.count(receipt) == 1
    assert "state/ceremony/wsc/y.json" in staged
    assert all("\\" not in p for p in staged)
