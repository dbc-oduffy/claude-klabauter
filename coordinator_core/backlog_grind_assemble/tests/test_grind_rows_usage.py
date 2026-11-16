"""run-record merges a `usage` block (scan_run shape, as_of run-record) beside `spend`."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.usage import run_locate
import pytest

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


def _patch_session(monkeypatch, sid) -> None:
    from coordinator_core.session import core
    monkeypatch.setattr(core, "resolve_session_id", lambda *_a, **_k: sid)


def test_located_run_dir_merges_usage_and_leaves_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wf = tmp_path / "wf_1"
    wf.mkdir()
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": "a1", "label": "ledger-sweep"}) + "\n", encoding="utf-8")
    _patch_session(monkeypatch, SID)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: tmp_path)
    seen = {}

    def fake_locate(session_dir, run_id, *, agent_descriptions) -> Path:
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


def test_located_run_dir_writes_a_spend_row_from_the_same_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wf = tmp_path / "wf_1"
    wf.mkdir()
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": "a1", "label": "ledger-sweep"}) + "\n", encoding="utf-8")
    _patch_session(monkeypatch, SID)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: tmp_path)
    monkeypatch.setattr(run_locate, "locate_run_dir", lambda *a, **k: wf)
    out = _run(tmp_path, monkeypatch, _record())
    rows = list((tmp_path / "state" / "spend").rglob(f"{RUN_ID}.json"))
    assert len(rows) == 1
    row = json.loads(rows[0].read_text(encoding="utf-8"))
    assert row["ceremony"] == "backlog-grind"
    assert row["agents"] == len(out["usage"]["agents"]) == 1
    assert {"tokens", "usd", "by_role"} <= set(row)


def test_absent_run_dir_writes_no_spend_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, SID)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: tmp_path)
    monkeypatch.setattr(run_locate, "locate_run_dir", lambda *a, **k: None)
    _run(tmp_path, monkeypatch, _record())
    assert not (tmp_path / "state" / "spend").exists()


def test_absent_run_dir_records_null_with_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, SID)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: tmp_path)
    monkeypatch.setattr(run_locate, "locate_run_dir", lambda *a, **k: None)
    out = _run(tmp_path, monkeypatch, _record())
    assert out["usage"] is None
    assert "not located" in out["usage_reason"]
    assert out["spend"] == {"output_tokens": 1}


def _transcript(path: Path, run_id: str, usage: dict) -> None:
    lines = [
        {"type": "user", "message": {"role": "user", "content": f"see scratch/warp/{run_id}/briefs/C1.md"}},
        {"type": "assistant", "requestId": "rq1",
         "message": {"id": "m1", "model": "claude-sonnet-5-5", "role": "assistant", "usage": usage}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def _settled_wf(session_dir: Path, name: str, agent_id: str, run_id: str, usage: dict) -> Path:
    wf = session_dir / "subagents" / "workflows" / name
    wf.mkdir(parents=True)
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": agent_id, "label": "work:C1"}) + "\n"
        + json.dumps({"type": "result", "agentId": agent_id}) + "\n", encoding="utf-8")
    _transcript(wf / f"agent-{agent_id}.jsonl", run_id, usage)
    return wf


def test_no_close_leg_agent_still_records_the_runs_own_four_totals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real locate_run_dir (no close-leg agent, all settled) returns None; the run-scoped scan
    still yields non-null usage. A wf dir that does not name the run contributes nothing."""
    _patch_session(monkeypatch, SID)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: tmp_path)
    _settled_wf(tmp_path, "wf_1", "a1", RUN_ID, {
        "input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40})
    _settled_wf(tmp_path, "wf_2", "a2", RUN_ID, {
        "input_tokens": 1, "output_tokens": 2,
        "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4})
    _settled_wf(tmp_path, "wf_other", "a3", "20260101T000000Z", {
        "input_tokens": 999, "output_tokens": 999,
        "cache_read_input_tokens": 999, "cache_creation_input_tokens": 999})
    assert run_locate.locate_run_dir(
        tmp_path, RUN_ID, agent_descriptions=grind_rows.GRIND_CLOSE_AGENT_DESCRIPTIONS) is None
    out = _run(tmp_path, monkeypatch, _record())
    total = out["usage"]["run_total"]
    assert (total["input_tokens"], total["output_tokens"],
            total["cache_read_input_tokens"], total["cache_creation_input_tokens"]) == (11, 22, 33, 44)
    assert sorted(a["agent_id"] for a in out["usage"]["agents"]) == ["a1", "a2"]
    assert out["usage"]["as_of"] == "run-record" and out["usage"]["unsettled"] == []
    assert "usage_reason" not in out
    assert out["spend"] == {"output_tokens": 1}


def test_run_needle_is_matched_only_within_the_transcript_head(tmp_path: Path) -> None:
    needle = f"scratch/warp/{RUN_ID}/"
    inside, beyond = tmp_path / "wf_in", tmp_path / "wf_beyond"
    for wf in (inside, beyond):
        wf.mkdir()
    pad = "x" * grind_rows._RUN_NEEDLE_HEAD_BYTES
    (inside / "agent-a.jsonl").write_text(needle + pad, encoding="utf-8")
    (beyond / "agent-a.jsonl").write_text(pad + needle, encoding="utf-8")
    assert grind_rows._names_run(inside, needle) is True
    assert grind_rows._names_run(beyond, needle) is False


def test_scan_failure_never_fails_the_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, SID)
    monkeypatch.setattr(run_locate, "session_dir_for", lambda sid, root=None: tmp_path)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(run_locate, "locate_run_dir", boom)
    out = _run(tmp_path, monkeypatch, _record())
    assert out["usage"] is None and "RuntimeError" in out["usage_reason"]


def test_no_session_leaves_record_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, None)
    assert _run(tmp_path, monkeypatch, _record()) == _record()


def test_caller_supplied_usage_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, SID)
    out = _run(tmp_path, monkeypatch, {**_record(), "usage": {"mine": 1}})
    assert out["usage"] == {"mine": 1}


def test_prewrite_commit_params_is_in_the_verb_usage_and_requires_its_flags(capsys: pytest.CaptureFixture[str]) -> None:
    assert grind_rows.main([]) == grind_rows.EXIT_USAGE
    assert "prewrite-commit-params" in capsys.readouterr().err
    assert grind_rows.main(["prewrite-commit-params", "--repo-root", "r"]) == grind_rows.EXIT_USAGE
