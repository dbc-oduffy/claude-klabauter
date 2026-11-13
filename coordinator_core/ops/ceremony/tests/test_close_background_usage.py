"""close.background writes the run's token figures into the warp usage.json, keeping as_of."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "warp-usage-20261113T000000-abc123"
SID = "11111111-2222-3333-4444-555555555555"


def _transcript_line(run_id: str) -> str:
    return json.dumps({"message": {"content": f"x scratch/warp/{run_id}/terminal-commit-params.json"}}) + "\n"


def _assistant(tokens_in: int) -> str:
    usage = {"input_tokens": tokens_in, "output_tokens": 5, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    return json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5-5", "usage": usage}}) + "\n"


def _session(tmp_path: Path, monkeypatch, *, settled: bool) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    wf = tmp_path / "cfg" / "projects" / "slug" / SID / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    events = [{"type": "started", "agentId": "a1", "label": "commit"}]
    if settled:
        events.append({"type": "result", "agentId": "a1"})
    (wf / "journal.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    (wf / "agent-a1.meta.json").write_text(json.dumps({"description": "commit"}), encoding="utf-8")
    (wf / "agent-a1.jsonl").write_text(_transcript_line(RUN) + _assistant(1000), encoding="utf-8")
    return wf


def _ctx(tmp_path: Path, record_dir: bool = True) -> cb.JobContext:
    root = tmp_path / "repo"
    if record_dir:
        d = root / RECORD_DIR_ROOT / RUN
        d.mkdir(parents=True)
        (d / "usage.json").write_text(json.dumps({"as_of": "terminal-commit", "unsettled": []}), encoding="utf-8")
    else:
        root.mkdir()
    return cb.JobContext(root, root / ".git", RUN, (), None, record={"session_id": SID})


def _usage(ctx: cb.JobContext) -> dict:
    return json.loads((ctx.root / RECORD_DIR_ROOT / RUN / "usage.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("settled", [False, True])
def test_usage_json_gains_figures_and_keeps_as_of(tmp_path, monkeypatch, settled):
    _session(tmp_path, monkeypatch, settled=settled)
    ctx = _ctx(tmp_path)
    cb._record_usage(ctx)
    body = _usage(ctx)
    assert body["as_of"] == "terminal-commit"
    assert body["run_total"]["total_tokens"] > 0
    assert body["by_role"] and len(body["agents"]) == 1
    assert f"{RECORD_DIR_ROOT}/{RUN}/usage.json" in ctx.written
    spend = [p for p in ctx.written if p.startswith("state/spend/")]
    assert len(spend) == 1
    row = json.loads((ctx.root / spend[0]).read_text(encoding="utf-8"))
    assert row["ceremony"] == "emit" and row["agents"] == 1 and row["tokens"] == body["run_total"]["total_tokens"]


def test_unlocated_dir_records_reason_and_keeps_as_of(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    ctx = _ctx(tmp_path)
    cb._record_usage(ctx)
    body = _usage(ctx)
    assert body["as_of"] == "terminal-commit"
    assert "run_total" not in body and body["usage_reason"]
    assert not [p for p in ctx.written if p.startswith("state/spend/")]


def test_second_run_does_not_rescan(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, settled=False)
    ctx = _ctx(tmp_path)
    cb._record_usage(ctx)
    first = _usage(ctx)
    monkeypatch.setattr("coordinator_core.usage.agent_usage.scan_run", lambda _d: pytest.fail("rescanned"))
    cb._record_usage(ctx)
    assert _usage(ctx) == first


def test_run_without_record_dir_writes_nothing(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, settled=False)
    ctx = _ctx(tmp_path, record_dir=False)
    cb._record_usage(ctx)
    assert ctx.written == []


def test_other_runs_transcript_is_not_matched(tmp_path, monkeypatch):
    wf = _session(tmp_path, monkeypatch, settled=True)
    (wf / "agent-a1.jsonl").write_text(_transcript_line("warp-other-run"), encoding="utf-8")
    ctx = _ctx(tmp_path)
    cb._record_usage(ctx)
    assert "run_total" not in _usage(ctx)
