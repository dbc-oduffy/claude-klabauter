"""run-record merges a `usage` block (scan_run shape, as_of run-record) beside `spend`."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.usage import run_locate

RUN_ID = "20261017T000000Z"
SID = "11111111-2222-3333-4444-555555555555"


def _record() -> dict:
    return {"profile": "bug", "run_id": "r", "counts": {}, "spend": {"output_tokens": 1},
            "handed_back": [], "halted": False}


def _run(tmp_path: Path, monkeypatch, record: dict) -> dict:
    rec = tmp_path / "record.json"
    rec.write_text(json.dumps(record), encoding="utf-8")
    code = grind_rows.main([
        "run-record", "--profile", "bug", "--run-id", RUN_ID,
        "--record-file", str(rec), "--repo-root", str(tmp_path),
    ])
    assert code == grind_rows.EXIT_OK
    target = tmp_path / "state" / "queue-grind" / "bug" / "runs" / f"{RUN_ID}.json"
    return json.loads(target.read_text(encoding="utf-8"))


def _patch_session(monkeypatch, sid):
    from coordinator_core.session import core
    monkeypatch.setattr(core, "resolve_session_id", lambda *_a, **_k: sid)


def test_located_run_dir_merges_usage_and_leaves_spend(tmp_path, monkeypatch):
    wf = tmp_path / "wf_1"
    wf.mkdir()
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": "a1", "label": "ledger-sweep"}) + "\n", encoding="utf-8")
    _patch_session(monkeypatch, SID)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: tmp_path)
    seen = {}

    def fake_locate(session_dir, run_id, *, agent_descriptions):
        seen["descriptions"] = agent_descriptions
        return wf

    monkeypatch.setattr(run_locate, "locate_run_dir", fake_locate)
    out = _run(tmp_path, monkeypatch, _record())
    assert out["spend"] == {"output_tokens": 1}
    assert out["usage"]["as_of"] == "run-record"
    assert out["usage"]["unsettled"] == ["a1"]
    assert [a["agent_id"] for a in out["usage"]["agents"]] == ["a1"]
    assert "run_total" in out["usage"] and "by_model" in out["usage"]
    assert seen["descriptions"] == grind_rows.GRIND_CLOSE_AGENT_DESCRIPTIONS


def test_absent_run_dir_records_null_with_reason(tmp_path, monkeypatch):
    _patch_session(monkeypatch, SID)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: tmp_path)
    monkeypatch.setattr(run_locate, "locate_run_dir", lambda *a, **k: None)
    out = _run(tmp_path, monkeypatch, _record())
    assert out["usage"] is None
    assert "not located" in out["usage_reason"]
    assert out["spend"] == {"output_tokens": 1}


def test_scan_failure_never_fails_the_record(tmp_path, monkeypatch):
    _patch_session(monkeypatch, SID)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: tmp_path)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(run_locate, "locate_run_dir", boom)
    out = _run(tmp_path, monkeypatch, _record())
    assert out["usage"] is None and "RuntimeError" in out["usage_reason"]


def test_no_session_leaves_record_untouched(tmp_path, monkeypatch):
    _patch_session(monkeypatch, None)
    assert _run(tmp_path, monkeypatch, _record()) == _record()


def test_caller_supplied_usage_is_kept(tmp_path, monkeypatch):
    _patch_session(monkeypatch, SID)
    out = _run(tmp_path, monkeypatch, {**_record(), "usage": {"mine": 1}})
    assert out["usage"] == {"mine": 1}
