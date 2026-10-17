"""terminal_commit._write_usage: usage.json in the warp record dir and the usage_total reply."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

SID = "f3dca0d5-7375-4313-83e8-a173943c2733"
RUN = "ask-20261017T000000-abc123"


def _msg(mid, model, inp, out):
    return json.dumps({
        "requestId": mid,
        "message": {"id": mid, "model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    }) + "\n"


def _stage(tmp_path: Path, monkeypatch, *, ambiguous=False) -> Path:
    cfg = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    wf = cfg / "projects" / "slug" / SID / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    prompt = json.dumps({"message": {"content": f"x scratch/warp/{RUN}/terminal-commit-params.json"}}) + "\n"
    (wf / "agent-c1.meta.json").write_text(json.dumps({"description": "commit"}), encoding="utf-8")
    (wf / "agent-c1.jsonl").write_text(prompt, encoding="utf-8")
    (wf / "agent-w1.jsonl").write_text(_msg("m1", "claude-opus-5-5", 1000, 500), encoding="utf-8")
    events = [
        {"type": "started", "agentId": "w1", "label": "work"},
        {"type": "result", "agentId": "w1"},
        {"type": "started", "agentId": "c1", "label": "commit"},
    ]
    (wf / "journal.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    if ambiguous:
        wf2 = wf.parent / "wf_2"
        wf2.mkdir()
        for p in wf.iterdir():
            (wf2 / p.name).write_bytes(p.read_bytes())
    root = tmp_path / "repo"
    root.mkdir()
    return root


def test_usage_json_written_and_total_returned(tmp_path, monkeypatch):
    root = _stage(tmp_path, monkeypatch)
    reply = tc._write_usage(root, SID, RUN)
    body = json.loads((root / RECORD_DIR_ROOT / RUN / "usage.json").read_text(encoding="utf-8"))
    assert body["as_of"] == "terminal-commit"
    assert body["unsettled"] == ["c1"]
    assert {a["agent_id"] for a in body["agents"]} == {"w1", "c1"}
    total = body["run_total"]
    assert total["total_tokens"] == 1500 and total["usd"] > 0
    assert reply == {"usage_total": {"total_tokens": 1500, "usd": total["usd"]}}


def test_ambiguous_run_dir_writes_nothing_and_does_not_fail(tmp_path, monkeypatch):
    root = _stage(tmp_path, monkeypatch, ambiguous=True)
    reply = tc._write_usage(root, SID, RUN)
    assert reply["usage_total"] is None and reply["usage_reason"]
    assert not (root / RECORD_DIR_ROOT / RUN / "usage.json").exists()


@pytest.mark.parametrize("sid", [None, "", "no-such-session"])
def test_unlocatable_session_is_null_with_reason(tmp_path, monkeypatch, sid):
    root = _stage(tmp_path, monkeypatch)
    reply = tc._write_usage(root, sid, RUN)
    assert reply["usage_total"] is None and reply["usage_reason"]
    assert not (root / RECORD_DIR_ROOT / RUN / "usage.json").exists()
